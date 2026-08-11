"""Conversation-log drain. Reads the spool, writes MongoDB, deletes on ack.

Replaces the Kafka consumer. The spool is the queue: durable, with an explicit
ack (delete after MongoDB confirms). Nothing is deleted before the write is
confirmed, so a drain killed mid-run re-ships at most one duplicate and loses
nothing.

Record building and redaction are imported from conversation_log_record --
credentials and profanity redaction are security behaviour and must not be
reimplemented here.

The timestamp comes from the spool filename, which is where the writer recorded
it. The writer never parses the payload, so the filename is the only place the
receipt time exists.
"""

from __future__ import annotations

import faulthandler
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]

sys.path.insert(0, str(_HERE))
# chathealthy-lib is not pip-installed in the workstation venv; every
# repo script reaches it by path. The service runs as LocalSystem with no
# working directory of ours, so this cannot be left to the caller.
sys.path.insert(0, str(_REPO_ROOT / "ChatHealthyLib" / "src"))

# CHLS binds its destination on first use. stderr only: this process's
# identity is DevOpsUser, which can write ClaudeCodeUtterances and nothing
# else, so a Mongo log handler would fail authorisation on every record. The
# supervisor captures both streams into the Windows event log.
os.environ.setdefault("CH_LOG_DESTINATION", "stderr")
os.environ.setdefault("CH_SPACE_NAME", "conversation-log-drain")
os.environ.setdefault("CH_COMPONENT", "conversation_log_drain")

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

import conversation_log_record as record  # noqa: E402
from conversation_log_writer import READ_PREFIX, SUFFIX, WRITE_PREFIX, spool_dir  # noqa: E402
from conversation_log_sweep import PURGE_AGE_HOURS, purge  # noqa: E402
from conversation_log_alarm import clear_drain_failure, record_drain_failure  # noqa: E402

# The archive is business data, not a log: its own database, its own identity.
IDENTITY = "DevOpsUser"
CLUSTER = "admin"
MONGO_DB = "ClaudeCodeUtterances"
MONGO_COLLECTION = "utterances"

_log_service = ChatHealthyLoggingService()


def _log(message: str) -> None:
    """Routine progress. INFO through the canonical logging service."""
    _log_service.info("drain: %s", message)


def _debug(message: str) -> None:
    """Routine detail. Emitted only when CH_LOG_LEVEL=DEBUG, which is the
    mechanism the logging service already provides -- this module previously
    carried its own CH_DRAIN_VERBOSE flag doing the same job."""
    _log_service.debug("drain: %s", message)


def _fail(message: str, exc: Exception | None = None) -> None:
    """Something went wrong. ERROR through the canonical logging service.

    exc= must be a ChatHealthyException per Rule-005 statement 2, so an
    external exception is converted at this boundary rather than passed raw.
    """
    _log_service.error(
        "drain: %s", message,
        exc=ChatHealthyException(
            mode="conversation_log_drain_failure",
            component="ConversationLogDrain",
            message=message,
            exception=exc,
        ),
    )


POLL_SECONDS = 5
# A write-* file older than this was left by a worker that died mid-write.
ORPHAN_AGE_SECONDS = 300


def _enable_debugging() -> None:
    """Make this process inspectable while it is running.

    The drain runs under a Windows service as LocalSystem, so there is no
    console to break into and no obvious way to ask a wedged process what it
    is doing. On 2026-08-08 the only way to find out was to run a second copy
    by hand under faulthandler. That should not be the procedure.

    faulthandler: always on. Costs nothing, and turns a hard crash or a
    deadlock into a stack trace on stderr, which the supervisor captures into
    the event log.

    CH_DRAIN_DEBUG_PORT: when set, waits for a debugger to attach on that port
    before doing any work. Absent -- the normal case -- nothing happens.
    """
    faulthandler.enable(file=sys.stderr)
    # faulthandler.register() is Unix-only -- it does not exist on Windows, so
    # there is no signal that dumps stacks on demand here. debugpy below is the
    # substitute: attach and look, rather than signal and read.
    if hasattr(faulthandler, "register") and hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)

    port = os.environ.get("CH_DRAIN_DEBUG_PORT", "").strip()
    if not port.isdigit():
        return
    try:
        import debugpy
        debugpy.listen(("127.0.0.1", int(port)))
        _log(f"waiting for a debugger to attach on 127.0.0.1:{port}")
        debugpy.wait_for_client()
        _log("debugger attached")
    except Exception as exc:
        _fail(f"debugpy unavailable ({type(exc).__name__}: {exc}); continuing")


def parse_spool_name(filename: str) -> tuple[str, int] | None:
    """read-<guid>-<time_ns>.txt -> (guid, time_ns). None if it does not match.

    Malformed names are skipped rather than guessed at: the time is the record's
    timestamp, and inventing one would silently corrupt the archive.
    """
    if not filename.startswith(READ_PREFIX) or not filename.endswith(SUFFIX):
        return None
    stem = filename[len(READ_PREFIX):-len(SUFFIX)]
    guid, _, time_part = stem.rpartition("-")
    if not guid or not time_part.isdigit():
        return None
    return guid, int(time_part)


def ready_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return [p for p in directory.iterdir()
            if p.is_file() and parse_spool_name(p.name) is not None]


def oldest_ready_age_seconds(directory: Path, now_ns: int) -> float:
    """Age of the oldest unshipped file. This is the pipeline's health signal --
    it goes bad whether the drain is dead, wedged, or MongoDB is unreachable,
    without the supervisor needing to know which.

    The clock is passed in. Reading it here would mean a branch for the tests.
    """
    ages = [(now_ns - parse_spool_name(p.name)[1]) / 1e9 for p in ready_files(directory)]
    return max(ages) if ages else 0.0


def sweep_orphans(directory: Path, max_age_seconds: int = ORPHAN_AGE_SECONDS) -> int:
    """Delete write-* files left by workers that died mid-write. These are never
    visible to the drain, so they are garbage rather than pending work."""
    if not directory.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for path in directory.iterdir():
        if not path.name.startswith(WRITE_PREFIX):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# Written by the Kafka-era consumer, carried by no reader, dropped on the port.
DROPPED_FIELDS = ("_version", "_framework", "_build")

def build_record(raw: bytes, time_ns: int, prior_collection, oai_client=None):
    """Parse the payload and build the archive record.

    `timestamp` is when the utterance was made, taken from the spool filename
    the writer stamped the instant the hook fired.

    The model client is threaded through because profanity is the one redaction
    matching cannot do. Omitting it does not fail -- it silently archives the
    profanity, which is how it went unredacted before.
    """
    payload = record._parse_hook_payload(raw)
    doc, credentials_error = record.build(payload, prior_collection, oai_client)
    if doc is None:
        return None, credentials_error
    for field in DROPPED_FIELDS:
        doc.pop(field, None)
    # Native date, not a formatted string. An index over it compares instants,
    # so ordering holds through daylight-saving transitions.
    doc["timestamp"] = datetime.fromtimestamp(time_ns / 1e9, tz=timezone.utc)
    _classify_into(doc)
    return doc, credentials_error


def _classify_into(doc: dict) -> None:
    """Add tone and utterance_class, if classification is available.

    Never raises. An unclassified utterance is still the utterance; losing it
    to a model outage would trade the record for a label. The absence of
    classified_at is how a later pass finds what was missed.
    """
    try:
        from conversation_log_classifier import CLASSIFIER_VERSION, classify
        result = classify(doc.get("content", ""))
        if result is None:
            return
        doc["tone"] = result.tone.value
        doc["utterance_class"] = result.utterance_class.value
        doc["classified_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        doc["classifier_version"] = CLASSIFIER_VERSION
    except Exception as exc:
        _fail(f"classification unavailable, archiving unclassified: "
              f"{type(exc).__name__}: {exc}", exc)


def drain_once(directory: Path, collection, oai_client=None) -> tuple[int, int]:
    """Ship every ready file. Returns (shipped, failed).

    One file at a time, deleting only after the insert is acknowledged. Batching
    would be faster but would make a mid-batch failure ambiguous about which
    files had landed.
    """
    shipped = failed = 0
    last_error = ""
    for path in sorted(ready_files(directory)):
        parsed = parse_spool_name(path.name)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            failed += 1
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        try:
            doc, _ = build_record(raw, parsed[1], collection, oai_client)
            if doc is None:
                path.unlink()          # nothing to archive; not an error
                continue
            collection.insert_one(doc)
        except Exception as exc:
            failed += 1                # file stays; retried next pass
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            continue
        try:
            path.unlink()
        except OSError:
            pass
        shipped += 1

    # This process is the only one that talks to MongoDB, so it is the only one
    # that can say a write failed -- and it runs in Session 0, so it is the one
    # that cannot say it to the operator. It records; the writer displays.
    if failed:
        record_drain_failure(last_error or "shipment failed")
    else:
        # Cleared on ANY pass without failures, including a pass with nothing
        # to ship. Clearing only after a successful shipment left the marker
        # standing once the spool emptied, so the writer kept alarming about a
        # failure that was already over.
        clear_drain_failure()
    return shipped, failed


def sweep_archive(collection) -> int:
    """Re-redact every archived document against the CURRENT secret list.

    CREDENTIALS ONLY, and deliberately: this walks the whole collection, so it
    takes no model client. Passing one turned a fast deterministic pass into
    one OpenAI call per document -- 39,000 serial HTTP calls that hung the
    drain for hours. Profanity is per-utterance work and already runs on the
    ingest path, where it costs one call per new utterance.

    Why it is needed: .env grows. A credential archived before anyone thought
    to record it is not redactable at the time and becomes redactable later.
    Without this, the archive keeps every secret that was ever typed before it
    was known. It also repairs anything written while redaction was broken.

    Only documents that actually change are written.
    """
    total = collection.count_documents({})
    _log(f"archive sweep BEGIN documents={total} "
         f"llm={'on' if oai_client else 'off'}")
    started = time.time()
    repaired = seen = 0
    for doc in collection.find({}, {"content": 1}):
        seen += 1
        if seen % 500 == 0:
            rate = seen / max(time.time() - started, 0.001)
            _debug(f"archive sweep {seen}/{total} repaired={repaired} "
                 f"{rate:.1f} docs/s eta={(total-seen)/max(rate,0.001):.0f}s")
        original = doc.get("content")
        if not isinstance(original, str) or not original:
            continue
        cleaned = record.redact(original)
        if cleaned != original:
            collection.update_one({"_id": doc["_id"]}, {"$set": {"content": cleaned}})
            repaired += 1
    _log(f"archive sweep END repaired={repaired} elapsed={time.time()-started:.0f}s")
    return repaired


def run_drain() -> int:
    _enable_debugging()
    # The service runs this as LocalSystem, whose environment carries none of
    # the vault settings. Without this the certificate fetch fails immediately
    # under the service while working perfectly from an operator shell.
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    # No connection string. The identity's certificate is the credential, and
    # there is no fallback: if the certificate cannot be obtained this raises
    # rather than reaching for a shared one.
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

    _log(f"starting identity={IDENTITY} cluster={CLUSTER} "
         f"target={MONGO_DB}.{MONGO_COLLECTION} poll={POLL_SECONDS}s")
    mongo_client = ChatHealthyMongoUtilities().getConnection(IDENTITY, CLUSTER)
    collection = mongo_client[MONGO_DB][MONGO_COLLECTION]
    _debug("connected")

    # Built once, not per record. Absent a key the drain still runs and still
    # redacts credentials -- profanity is the only thing lost, and losing the
    # utterance would be worse.
    oai_client = None
    try:
        from openai import OpenAI
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if api_key:
            oai_client = OpenAI(api_key=api_key)
        else:
            _fail("OPENAI_API_KEY absent: profanity will NOT be redacted")
    except Exception as exc:
        _fail(f"openai client unavailable: {exc}; profanity NOT redacted", exc)
    directory = spool_dir()
    # Seeded to today: a restart is not midnight, and must not trigger a sweep.
    last_sweep_day = datetime.now().date()
    last_heartbeat = time.monotonic()
    try:
      while True:
        try:
            # Before the work, never after: a secret added to .env mid-pass
            # would otherwise go unredacted for this entire batch.
            if record.refresh_secrets_if_changed():
                _debug("secret list reloaded (.env changed)")
            shipped, failed = drain_once(directory, collection, oai_client)
            orphans = sweep_orphans(directory)
            pending = len(ready_files(directory))
            # A pass with nothing to do says nothing. Logging every 5s put
            # 17,000 lines of shipped=0 into the event log daily and buried
            # the lines that mattered. Work is reported; idleness is not,
            # except for a heartbeat so silence still means something.
            now = time.monotonic()
            if failed:
                _fail(f"pass shipped={shipped} failed={failed} pending={pending}")
            elif shipped or orphans:
                _debug(f"pass shipped={shipped} pending={pending} "
                       f"orphans_removed={orphans}")


            # Every pass, not once a day. The rule is "deleted if it has not
            # reached the archive within 24 hours"; a daily sweep would let a
            # file that turned 24h old at 3pm survive until the next midnight,
            # which is up to 47 hours. Costs one directory listing.
            lost = purge(directory)
            if lost:
                _log(f"purge deleted {len(lost)} spooled utterance(s) older "
                     f"than {PURGE_AGE_HOURS}h. Those utterances are gone.")
                for name in lost:
                    _log(f"sweep: lost {name}")

            # If it is midnight, spawn the sweep. Nothing is remembered about
            # any sweep -- not whether one is running, not whether the last one
            # finished. It is idempotent, so a second one does no harm and a
            # missed one is caught tomorrow. The only thing tracked is the day,
            # which is what "is it midnight" means.
            today = datetime.now().date()
            if today != last_sweep_day:
                last_sweep_day = today
                threading.Thread(target=sweep_archive, args=(collection,),
                                 name="archive-sweep", daemon=True).start()
                _log("midnight - archive sweep spawned")
        except Exception as exc:                      # never let the loop die
            _fail(f"pass FAILED {type(exc).__name__}: {exc}", exc)
        time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        # The supervisor hard-kills this process, so no finally would run
        # under the service. It logs the stop itself; see Worker.KillDrain.
        pass


if __name__ == "__main__":
    sys.exit(run_drain())
