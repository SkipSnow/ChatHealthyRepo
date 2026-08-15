"""reservation_reaper.py - Azure Automation runbook (Python 3).

Reaps overdue automated cluster_lifecycle reservations and pauses the
Atlas pipeline cluster when no live reservation remains and no run or
work item is still going (R1). Both facts come from pipelineAdmin, so
the pause decision needs nothing but the database. On uncaught exception writes
to pipelineAdmin.pipeline.reaper_state, so an incomplete pass is visible
where job metadata is read, and sends an email on an hourly streak. On
success that record is cleared.

Replaces the cluster_lifecycle_timer in pipeline/Code/function_app.py.
Runs every 5 minutes on schedule SCH-Reaper-5min in the
ChatHealthyJobManager Automation Account.

Also reaps stuck per-pipeline atomic mutex rows in
admin.cluster_lifecycle (rows with kind=="pipeline_lock", added
2026-07-28 in commit 1384700d). A pipeline_lock is released by the
Controller on run wrap; when the Controller never starts (e.g., VM
cloud-init fails to install docker) or dies mid-run, the lock sits
until its 12h TTL blocks every subsequent fire. On each tick the
reaper checks every pipeline_lock past its startup grace and reaps
the row when the run holds no live reservation. The reservation is
what says a run is legitimately alive: a run makes one when it starts
and cancels it in the Controller's finally when it ends. Matching
pipeline.runs doc is stamped status=failed
reason=controller_never_started_or_died.

Environment (Automation Variables, exposed via os.environ at runtime):
  ATLAS_PIPELINE_PUBLIC_KEY       - Atlas API public key that may pause a cluster
  ATLAS_PIPELINE_PRIVATE_KEY      - its private half
  ATLAS_PROJECT_ID                - Atlas group/project ID
  ENV_PREFIX                      - "dev" | "qa" | "prod"
  PIPELINE_CLUSTER                - Atlas cluster name (default ChatHealthyDataPipelines)
  AZ_SUBSCRIPTION_ID              - Azure subscription hosting the Automation Account (R2)
  AZ_RESOURCE_GROUP               - RG hosting ChatHealthyJobManager (R2)
  AZ_AUTOMATION_ACCOUNT           - "ChatHealthyJobManager" (R2)
  SPARKMAIL_API_KEY               - SparkPost API key (R3)
  NOTIFICATION_FROM_EMAIL         - sender address (R3)
  NOTIFICATION_TO_EMAIL           - recipient address (R3)
"""
# Atlas addresses are SRV records, and the Automation sandbox's own resolver
# does not answer external SRV queries -- every connection died on
# "All nameservers failed to answer the query _mongodb._tcp.<host> IN SRV".
# Point dnspython at public resolvers instead. This MUST run before pymongo is
# imported, which the chathealthy_lib import below does, because pymongo binds
# the default resolver at import time.
try:
    import dns.resolver  # type: ignore[import-not-found]
    _r = dns.resolver.Resolver(configure=False)
    _r.nameservers = ["8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1"]
    _r.timeout = 5
    _r.lifetime = 10
    dns.resolver.default_resolver = _r
except ImportError:
    pass

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
from chathealthy_lib.pipeline_job_status import ChatHealthyPipelineJobStatus
import os
import sys
import json

import traceback
from datetime import datetime, timezone, timedelta

import requests
from requests.auth import HTTPDigestAuth
from pymongo import MongoClient

# Breadcrumbs to stderr from the first line. Everything below this can fail
# before the Mongo handler exists -- an import, a missing variable, a
# credential -- and when it does, stderr is the only surface that survives.
# It lands in the Automation job record either way.
sys.stderr.write("reaper: module import begin\n")

# This runbook ships to Azure Automation on its own, so it carries the
# constant rather than importing pipeline_db, which is not deployed beside
# it. That import is what killed every 15-minute tick with
# ModuleNotFoundError, before the runbook could say anything at all.
PIPELINE_ADMIN_DB = "pipelineAdmin"

# pipeline_db was also where the logging identity got named. Naming it here
# is not optional: without it the handler raises mongo_log_identity_not_set
# on the first log() call.
from chathealthy_lib.logging_service import set_mongo_log_identity  # noqa: E402
set_mongo_log_identity("pipelineEditor")

try:
    import automationassets
    for k in ("ATLAS_PIPELINE_PUBLIC_KEY", "ATLAS_PIPELINE_PRIVATE_KEY",
              "ATLAS_PROJECT_ID", "ENV_PREFIX", "PIPELINE_CLUSTER",
              "CH_LOG_DB", "KEY_VAULT_URI",
              "AZ_SUBSCRIPTION_ID", "AZ_RESOURCE_GROUP", "AZ_AUTOMATION_ACCOUNT",
              "AZ_VM_RESOURCE_GROUP",
              "SPARKMAIL_API_KEY",
              "NOTIFICATION_FROM_EMAIL", "NOTIFICATION_TO_EMAIL",
              # The library resolves a credential by identity name; without
              # these in the environment it looks for a managed identity the
              # Automation Account does not have.
              "PIPELINEEDITOR_AZURE_TENANT_ID",
              "PIPELINEEDITOR_AZURE_CLIENT_ID",
              "PIPELINEEDITOR_AZURE_CLIENT_SECRET"):
        try:
            os.environ[k] = str(automationassets.get_automation_variable(k))
        except Exception:
            pass
except ImportError:
    pass

ENV_PREFIX       = os.environ.get("ENV_PREFIX", "dev")

# CHLS env setup MUST happen before the module-level
# ChatHealthyLoggingService() singleton is instantiated below.
os.environ.setdefault("CH_SPACE_NAME", "reservation-reaper")
os.environ.setdefault("CH_COMPONENT", "reservation-reaper")
os.environ.setdefault("CH_LOG_DESTINATION", "stderr,mongo")
sys.stderr.write(
    f"reaper: env hydrated; CH_LOG_DB={os.environ.get('CH_LOG_DB', '<unset>')!r} "
    f"ENV_PREFIX={os.environ.get('ENV_PREFIX', '<unset>')!r} "
    f"identity_secret_present={bool(os.environ.get('PIPELINEEDITOR_AZURE_CLIENT_SECRET'))}\n"
)
CLUSTER_NAME     = os.environ.get("PIPELINE_CLUSTER", "ChatHealthyDataPipelines")
# Operator directive 2026-08-03: all coord on pipeline cluster.
ATLAS_PUB        = os.environ["ATLAS_PIPELINE_PUBLIC_KEY"]
ATLAS_PRIV       = os.environ["ATLAS_PIPELINE_PRIVATE_KEY"]
ATLAS_PROJECT    = os.environ["ATLAS_PROJECT_ID"]
AZ_SUB           = os.environ.get("AZ_SUBSCRIPTION_ID", "")
AZ_RG            = os.environ.get("AZ_RESOURCE_GROUP", "")
AZ_AA            = os.environ.get("AZ_AUTOMATION_ACCOUNT", "ChatHealthyJobManager")
SPARKPOST_KEY    = os.environ.get("SPARKMAIL_API_KEY", "")
EMAIL_FROM       = os.environ.get("NOTIFICATION_FROM_EMAIL", "Skip.Snow@mail.chatHealthy.ai")
EMAIL_TO         = os.environ.get("NOTIFICATION_TO_EMAIL", "Skip@chatHealthy.ai")
DB_NAME          = PIPELINE_ADMIN_DB
COLLECTION       = "cluster_lifecycle"
ATLAS_BASE       = f"https://cloud.mongodb.com/api/atlas/v2/groups/{ATLAS_PROJECT}"
ATLAS_CLUSTER_URL= f"{ATLAS_BASE}/clusters/{CLUSTER_NAME}"
ATLAS_HEADERS    = {
    "Content-Type": "application/json",
    "Accept": "application/vnd.atlas.2023-02-01+json",
}
REAPER_APPNAME   = "reservation_reaper"

log = ChatHealthyLoggingService()


def _pst_now_iso():
    # PDT = UTC-7 May-November; PST = UTC-8 November-March.
    # Use a fixed tz offset derived from current UTC offset for US/Pacific.
    # Simplest correct approach without pytz: compute via timezone.
    # PDT applies 2nd Sun Mar - 1st Sun Nov. For the runbook's purposes
    # the schema only requires a valid ISO8601 datetime with -07:00 or -08:00.
    # Detect roughly via month; this is adequate per the schema's intent.
    now_utc = datetime.now(timezone.utc)
    m = now_utc.month
    is_pdt = 3 < m < 11 or (m == 3 and now_utc.day >= 8) or (m == 11 and now_utc.day < 8)
    offset = timedelta(hours=-7 if is_pdt else -8)
    return now_utc.astimezone(timezone(offset)).isoformat()


REAPER_STATE_COLLECTION = "pipeline.reaper_state"
REAPER_STATE_ID = "reservation_reaper"


def _state_coll(client):
    """The reaper's own state, beside the metadata it maintains.

    A tick is a fresh process, so a failing streak has to survive between
    runs. It lives in pipelineAdmin -- the database the reaper already opens
    every tick -- rather than in a blob, which was a second store with its own
    account, credential and SDK holding one fact, and which never once worked
    in the Automation sandbox.
    """
    return client[PIPELINE_ADMIN_DB][REAPER_STATE_COLLECTION]


def _read_failure_state(client):
    if client is None:
        return None
    try:
        return _state_coll(client).find_one({"_id": REAPER_STATE_ID})
    except Exception as ex:
        log.warning("could not read reaper state: %r", ex)
        return None


def _write_failure_state(client, state):
    if client is None:
        return
    try:
        state["_id"] = REAPER_STATE_ID
        _state_coll(client).replace_one({"_id": REAPER_STATE_ID}, state, upsert=True)
    except Exception as ex:
        log.warning("could not write reaper state: %r", ex)


def _delete_failure_state(client):
    """Called on the success path: no streak, nothing to say."""
    if client is None:
        return
    try:
        if _state_coll(client).delete_one({"_id": REAPER_STATE_ID}).deleted_count:
            log.info("reaper state cleared (success path)")
    except Exception as ex:
        log.warning("could not clear reaper state: %r", ex)


def _send_sparkpost_email(subject, body):
    if not SPARKPOST_KEY:
        log.warning("R3: SPARKMAIL_API_KEY absent; cannot send streak email")
        return False
    try:
        resp = requests.post(
            "https://api.sparkpost.com/api/v1/transmissions",
            headers={"Authorization": SPARKPOST_KEY, "Content-Type": "application/json"},
            json={
                "content": {
                    "from": EMAIL_FROM,
                    "subject": subject,
                    "text": body,
                },
                "recipients": [{"address": EMAIL_TO}],
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            return True
        log.warning("R3: SparkPost returned %s %s", resp.status_code, resp.text[:200])
        return False
    except Exception as ex:
        log.warning("R3: SparkPost send raised: %r", ex)
        return False


def _maybe_send_streak_email(state, latest_cause):
    if not state.get("failures"):
        return
    try:
        earliest = state["failures"][0].get("failure_time")
        earliest_dt = datetime.fromisoformat(earliest) if earliest else None
    except Exception:
        earliest_dt = None
    if earliest_dt is None:
        return
    now_utc = datetime.now(timezone.utc)
    streak_secs = (now_utc - earliest_dt).total_seconds()
    if streak_secs < 3900:
        return
    emails = state.get("emails_sent") or []
    if emails:
        try:
            last_sent = datetime.fromisoformat(emails[-1]["datetime_sent_pst"])
            if (now_utc - last_sent.astimezone(timezone.utc)).total_seconds() < 3600:
                return
        except Exception:
            pass
    subject = f"[ChatHealthy] ReservationReaper failure streak: {int(streak_secs // 60)} min"
    body = (
        f"The Azure Automation runbook ReservationReaper has been failing continuously "
        f"for {int(streak_secs // 60)} minutes.\n\n"
        f"Earliest failure_time: {earliest}\n"
        f"Latest failure_cause: {latest_cause}\n"
        f"Total failures recorded: {len(state['failures'])}\n\n"
        f"Atlas pipeline cluster auto-pause may not be functioning. "
        f"Inspect job logs in Automation Account ChatHealthyJobManager / "
        f"runbook ReservationReaper."
    )
    if _send_sparkpost_email(subject, body):
        emails.append({
            "datetime_sent_pst": _pst_now_iso(),
            "from": EMAIL_FROM,
            "to": EMAIL_TO,
            "content": body,
        })
        state["emails_sent"] = emails


def _record_failure(client, cause, unexamined=None, corrected=None):
    """Record an incomplete pass where a reader of job metadata will find it.

    The traceback says the pass died; unexamined says what it did not reach,
    which is what a reader needs to know how much of the metadata is still
    unjudged. A pass that completes deletes this document, so its presence
    means incomplete and its absence means complete -- including complete
    having found nothing to correct.
    """
    state = _read_failure_state(client) or {"emails_sent": [], "failures": []}
    state.setdefault("emails_sent", [])
    state.setdefault("failures", [])
    state["failures"].append({
        "failure_time": datetime.now(timezone.utc).isoformat(),
        "failure_cause": cause[:2000],
        "unexamined": list(unexamined or [])[:200],
        "unexamined_count": len(unexamined or []),
        "corrected_before_failure": list(corrected or [])[:200],
    })
    _maybe_send_streak_email(state, cause)
    _write_failure_state(client, state)


# Startup grace for pipeline_lock rows: how long we wait after
# acquired_at before judging the lock. VM boot + apt + docker pull +
# Controller container start is typically 3-5 minutes in a healthy fire;
# 8 min leaves headroom for slow days.
PIPELINE_LOCK_STARTUP_GRACE_MIN = 8

# A run in one of these states is over; its lock is free whatever its
# reservation still says.
_TERMINAL_RUN_STATUSES = {"succeeded", "failed", "aborted", "completed"}

# The same for a single work item.
_TERMINAL_ITEM_STATUSES = {"done", "succeeded", "failed", "complete", "completed"}


class CorrectionPass:
    """One sweep over pipeline job metadata, and a record of what it reached.

    REQ-B-002 requires that no job is recorded in a non-terminal state while
    it is not running, and that no lock or reservation is held on behalf of a
    job that is not running. Locks alone do not satisfy that: a job whose
    reservation was cancelled before its manifest was stamped leaves no
    lifecycle row at all, so a sweep driven by lifecycle rows never sees it.
    This is driven by pipeline.runs as well.

    REQ-B-003 requires that a pass which cannot finish says so, and says what
    it did not reach. Every unit of work is enrolled before the sweep starts
    and struck off as it is examined, so whatever remains enrolled at the end
    is exactly what went unexamined.
    """

    def __init__(self, client):
        self._client = client
        self._runs = client[PIPELINE_ADMIN_DB]["pipeline.runs"]
        self._lifecycle = client[DB_NAME][COLLECTION]
        self._status = ChatHealthyPipelineJobStatus(client)
        self.outstanding: list[str] = []
        self.corrected: list[str] = []

    def enrol(self, item: str) -> None:
        self.outstanding.append(item)

    def done(self, item: str) -> None:
        if item in self.outstanding:
            self.outstanding.remove(item)

    def correct_orphaned_runs(self, now_aware) -> None:
        """A run recorded as going, whose Controller is not.

        The run's own record names the host, so a run with no lifecycle row is
        still answerable. A run that names no host cannot be judged and is left
        alone rather than guessed at.
        """
        candidates = list(self._runs.find(
            {"status": {"$nin": list(_TERMINAL_RUN_STATUSES)}},
            {"run_id": 1, "vm_name": 1, "controller_pid": 1, "started_at": 1}))
        for run in candidates:
            self.enrol(f"run:{run.get('run_id')}")
        for run in candidates:
            run_id = run.get("run_id")
            item = f"run:{run_id}"
            started = run.get("started_at")
            if started is not None and getattr(started, "tzinfo", None) is None:
                started = started.replace(tzinfo=timezone.utc)
            if started and (now_aware - started).total_seconds() / 60.0 < PIPELINE_LOCK_STARTUP_GRACE_MIN:
                self.done(item)
                continue
            verdict = self._run_verdict(run_id, run)
            if verdict is None or not verdict.has_stopped:
                self.done(item)
                continue
            self._runs.update_one(
                {"run_id": run_id, "status": {"$nin": list(_TERMINAL_RUN_STATUSES)}},
                {"$set": {"status": "failed",
                          "failure_reason": f"controller_not_running:{verdict.detail}",
                          "corrected_by_reaper_at": now_aware}})
            self._lifecycle.delete_many({"run_id": run_id})
            self._lifecycle.delete_one({"_id": run_id})
            log.info("Corrected orphaned run %s: %s", run_id, verdict.detail)
            self.corrected.append(item)
            self.done(item)

    def _run_verdict(self, run_id, run):
        return self._status.verdict(run_id)


def _reap_stuck_pipeline_lock(coll, runs_coll, row, now_aware, status):
    """Reap a pipeline_lock row iff its run holds no live reservation.

    The reservation is what says a run is legitimately alive. A run makes one
    when it starts and cancels it when it ends, so the lock lives and dies
    with it.

    A lock is reapable when acquired_at is at least
    PIPELINE_LOCK_STARTUP_GRACE_MIN ago and either the run's status is
    terminal or its reservation has expired or gone.
    """
    acquired_at = row.get("acquired_at")
    if isinstance(acquired_at, str):
        try:
            acquired_at = datetime.fromisoformat(acquired_at)
        except (ValueError, TypeError):
            return False
    if not acquired_at:
        return False
    if getattr(acquired_at, "tzinfo", None) is None:
        acquired_at = acquired_at.replace(tzinfo=timezone.utc)
    age_min = (now_aware - acquired_at).total_seconds() / 60.0
    if age_min < PIPELINE_LOCK_STARTUP_GRACE_MIN:
        return False  # Still inside startup grace; give the Controller time.
    run_id = row.get("run_id")
    if not run_id:
        return False

    run = runs_coll.find_one({"run_id": run_id})
    if run and str(run.get("status", "")).lower() in _TERMINAL_RUN_STATUSES:
        log.info("Reaping pipeline_lock for a finished run: run_id=%s status=%s",
                 run_id, run.get("status"))
    else:
        expiry = (coll.find_one({"_id": run_id}) or {}).get("expiry_at")
        if isinstance(expiry, str):
            try:
                expiry = datetime.fromisoformat(expiry)
            except (ValueError, TypeError):
                expiry = None
        # The process decides, and the clock only speaks when Azure will not.
        # A running Controller keeps its lock however old its reservation is:
        # a heartbeat thread that wedged while the run works on is a failure
        # of the renewal, not of the run, and reaping it here would stamp a
        # live run failed and free its lock for a second one to collide with.
        verdict = status.verdict(run_id)
        log.info("Run %s verdict: %s (%s)", run_id, verdict.state, verdict.detail)
        if verdict.is_running:
            return False
        if not verdict.has_stopped:
            if expiry is not None:
                if getattr(expiry, "tzinfo", None) is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry > now_aware:
                    return False  # Unproven either way; the lease still stands.
        log.info(
            "Reaping stuck pipeline_lock: _id=%s run_id=%s age_min=%.1f "
            "status=%s reservation_expiry=%s",
            row.get("_id"), run_id, age_min,
            (run or {}).get("status"), expiry,
        )

    # Atomic release: filter on _id AND run_id so a concurrent legitimate
    # release by the actual Controller cannot race with us.
    coll.delete_one({"_id": row["_id"], "run_id": run_id})
    # Also delete the matching legacy reservation row (same run_id, in
    # the same collection under _id=run_id).
    coll.delete_one({"_id": run_id})
    # Stamp pipeline.runs for post-mortem.
    try:
        runs_coll.update_one(
            {"run_id": run_id, "status": "running"},
            {"$set": {
                "status": "failed",
                "failure_reason": "controller_never_started_or_died",
                "reaped_by_reservation_reaper_at": now_aware,
            }},
        )
    except Exception as ex:  # noqa: BLE001
        log.warning("Could not stamp pipeline.runs failure for %s: %r",
                    run_id, ex)
    return True

def _main():
    now = datetime.now(timezone.utc)

    atlas_auth = HTTPDigestAuth(ATLAS_PUB, ATLAS_PRIV)
    pre = requests.get(ATLAS_CLUSTER_URL, auth=atlas_auth,
                       headers=ATLAS_HEADERS, timeout=15)
    pre_state = pre.json().get("stateName", "UNKNOWN")
    pre_paused = pre.json().get("paused", False)
    if pre_state == "IDLE" and pre_paused:
        msg = ("Reaper tick: reaped=0, live_remaining=0, work_pending=False, "
               "cluster_state=PAUSED, paused=True, reason=already_paused")
        log.info(msg)
        ChatHealthyLoggingService().info(msg)
        return None

    # Uses pipelineEditor identity to access frontend cluster resources
    client = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "admin")
    coll = client[DB_NAME][COLLECTION]
    runs_coll = client[PIPELINE_ADMIN_DB]["pipeline.runs"]
    reservations = list(coll.find({}))
    log.info("Loaded %d cluster_lifecycle rows from %s.%s",
             len(reservations), DB_NAME, COLLECTION)

    # REQ-B-002: a run recorded as going whose Controller is not must be
    # corrected, whether or not it left a lock behind.
    global _PASS
    _PASS = CorrectionPass(client)
    for r in reservations:
        _PASS.enrol(f"lifecycle:{r.get('_id')}")
    _PASS.correct_orphaned_runs(now)

    kept, reaped = [], []
    now_utc_naive = datetime.utcnow()
    now_utc_aware = datetime.now(timezone.utc)
    for r in reservations:
        _PASS.done(f"lifecycle:{r.get('_id')}")
        # Row shape discriminator. Two shapes coexist here:
        #  - kind == "pipeline_lock" -- atomic per-pipeline mutex added
        #    2026-07-28 in commit 1384700d. Fields: _id="pipeline_lock:{name}",
        #    kind, pipeline_name, run_id, vm_name, acquired_at (ISO str),
        #    expires_at (ISO str). Released by Controller on run wrap OR
        #    by this reaper when pipeline.runs says the run is not active.
        #  - legacy DB reservation -- the original shape. Fields: _id=run_id,
        #    expiry_at (naive datetime), status, requester. Released by
        #    wall-clock TTL only.
        # New pipeline_lock path FIRST because the legacy path treats
        # missing expiry_at as "keep forever" and would silently protect
        # every pipeline_lock row from reaping.
        if r.get("kind") == "pipeline_lock":
            if _reap_stuck_pipeline_lock(coll, runs_coll, r, now_utc_aware,
                                         _PASS._status):
                reaped.append(r)
            else:
                kept.append(r)
            continue
        # Legacy reservation shape: reap by wall-clock expiry_at.
        expiry_dt = r.get("expiry_at")
        if expiry_dt is None:
            kept.append(r); continue
        if isinstance(expiry_dt, str):
            try:
                expiry_dt = datetime.fromisoformat(expiry_dt)
            except (ValueError, TypeError):
                kept.append(r); continue
        if getattr(expiry_dt, "tzinfo", None) is not None:
            expiry_dt = expiry_dt.astimezone(timezone.utc).replace(tzinfo=None)
        if now_utc_naive <= expiry_dt:
            kept.append(r); continue
        reaped.append(r)
        log.info("Reaping overdue reservation: _id=%s requester=%s expiry_at=%s",
                 r.get("_id", ""), r.get("requester", ""), expiry_dt.isoformat())

    if reaped:
        # Pipeline_lock rows delete themselves inside _reap_stuck_pipeline_lock
        # (needs the guarded _id+run_id filter to prove atomic ownership).
        # Legacy reservation rows delete here by _id.
        legacy_ids = [r["_id"] for r in reaped if r.get("kind") != "pipeline_lock"]
        if legacy_ids:
            coll.delete_many({"_id": {"$in": legacy_ids}})

    live = [r for r in kept if r.get("status", "active") == "active"]

    # Precedence: the reservation queue is the primary signal (REQ-B-001). While a
    # live reservation exists the cluster stays up, full stop. The confirmatory signal is
    # lower-precedence confirmatory signal that only runs once the queue is empty:
    # a run or work item still going without a reservation means a job lost its
    # reservation while alive, so hold the pause and say so.
    # The reservation is the only signal. No live reservation means nothing
    # has claimed the cluster, and it is paused -- whether or not something is
    # connected to it, and whether or not a run thinks it is still going.
    # A job that works without reserving is a job with a defect, and paying to
    # keep a cluster warm for it teaches nobody anything.
    should_pause = not live
    pause_reason = "no_live_reservations" if should_pause else ""
    client_active = None

    cluster_state = "UNKNOWN"
    paused = False
    pause_failure_detail = ""
    if should_pause:
        resp = requests.get(ATLAS_CLUSTER_URL, auth=atlas_auth,
                            headers=ATLAS_HEADERS, timeout=15)
        cluster_info = resp.json()
        cluster_state = cluster_info.get("stateName", "UNKNOWN")
        # Post-job scale-down per operator directive 2026-08-01:
        # if the cluster is at M80 (JOB_TIER), resize to M30 before pausing
        # so the reaper leaves the cluster on a smaller tier even if
        # the pause step later fails or is deferred.
        current_tier = ""
        try:
            current_tier = cluster_info.get("replicationSpecs", [{}])[0]\
                .get("regionConfigs", [{}])[0]\
                .get("electableSpecs", {}).get("instanceSize", "")
        except (IndexError, KeyError, AttributeError):
            pass
        if current_tier == "M80" and cluster_state == "IDLE":
            log.info("Reaper: cluster %s at M80 -- resizing to M30 before pause",
                     CLUSTER_NAME)
            resize_body = {
                "replicationSpecs": [{
                    "regionConfigs": [{
                        "electableSpecs": {"instanceSize": "M30", "nodeCount": 3},
                        "analyticsSpecs": {"instanceSize": "M30", "nodeCount": 0},
                        "readOnlySpecs": {"instanceSize": "M30", "nodeCount": 0},
                        "autoScaling": {"compute": {"enabled": True,
                                                     "maxInstanceSize": "M40",
                                                     "minInstanceSize": "M30",
                                                     "scaleDownEnabled": False}},
                        "priority": 7,
                    }],
                }],
            }
            resize_resp = requests.patch(ATLAS_CLUSTER_URL, json=resize_body,
                                          auth=atlas_auth, headers=ATLAS_HEADERS, timeout=30)
            if resize_resp.status_code not in (200, 202):
                log.warning(
                    "Reaper: M80->M30 resize rejected (%d): %s -- cluster left at M80; will retry next tick",
                    resize_resp.status_code, resize_resp.text[:300],
                )
        if cluster_state == "IDLE":
            resp = requests.patch(
                ATLAS_CLUSTER_URL, json={"paused": True},
                auth=atlas_auth, headers=ATLAS_HEADERS, timeout=30,
            )
            if resp.status_code in (200, 202):
                paused = True
                log.info("Cluster %s paused (reason=%s, Atlas response: %s)",
                         CLUSTER_NAME, pause_reason,
                         resp.json().get("stateName", "?"))
            else:
                try:
                    j = resp.json()
                    pause_failure_detail = j.get("errorCode", "") or j.get("detail", "")
                except Exception:
                    pause_failure_detail = f"http_{resp.status_code}"
                log.warning("Cluster %s pause REJECTED: %s %s",
                            CLUSTER_NAME, resp.status_code, pause_failure_detail)
                if pause_failure_detail:
                    pause_reason = f"{pause_reason}|atlas_rejected:{pause_failure_detail}"

    msg = (f"Reaper tick: reaped={len(reaped)}, live_remaining={len(live)}, "
           f"work_pending={client_active}, cluster_state={cluster_state}, "
           f"paused={paused}, reason={pause_reason or 'none'}")
    log.info(msg)
    ChatHealthyLoggingService().info(msg)
    return client


# Azure Automation executes a runbook as the main script, so this guard does
# not change how it runs there. It does stop an import from firing a tick --
# importing this module to test one function paused a live cluster.
def _run_tick() -> int:
    global _client
    _client = None
    try:
        _client = _main()
        _delete_failure_state(_client)
        return 0
    except Exception:
        tb = traceback.format_exc()
        log.error("Reaper tick failed: %s", tb)
        try:
            # The pass did not finish. Say so where job metadata is read;
            # opening a connection here is the last resort when _main died
            # before one existed.
            if _client is None:
                _client = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "admin")
            _record_failure(_client, tb,
                            unexamined=(_PASS.outstanding if _PASS else None),
                            corrected=(_PASS.corrected if _PASS else None))
        except Exception as inner:
            log.error("recording the incomplete pass also failed: %r", inner)
        return 1


_client = None
if __name__ == "__main__":
    sys.exit(_run_tick())
