"""Watch the Start At fire (2026-10-22): discover the run, feed the status log
every 5 minutes unconditionally, and print one line per state change so the
Monitor surfaces admission and terminal.

stdout lines (Monitor events): ADMITTED / STATUS-CHANGE / TERMINAL / WATCHER-ERROR.
The 5-minute tabular status goes to _oneshots/medicare_status.log via
checkPipelineStatus.py <run_id>.
"""
import datetime
import subprocess
import sys
import time

sys.path.insert(0, "ChatHealthyLib/src")
sys.path.insert(0, ".")
from pipeline.run_lifecycle.pipeline_env import load_pipeline_env  # noqa: E402

load_pipeline_env()

from chathealthy_lib.logging_service import (  # noqa: E402
    ChatHealthyLoggingService)

# The watch's event lines go through the one logging surface. They were
# print() because this program lived in _oneshots, where no rule reached
# it; the status report itself still goes to the destination file.
_log = ChatHealthyLoggingService()
from bson import ObjectId  # noqa: E402
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

# The destination is the only thing this program needs told. What it asks
# is fixed: it discovers the run itself, and the status API answers at a
# static address the controller NIC carries across runs. So one argument,
# and it is where to write.
_DEFAULT_LOG = "_oneshots/medicare_status.log"
LOG = _DEFAULT_LOG
# Runs admitted after this watch began, not after a date written into the
# source. The constant used to be the wall-clock of one particular fire
# (2026-10-25 13:51), so every later watch asked for "the newest run since
# four days ago" and latched the most recent TERMINAL run -- on 2026-10-29
# it reported ADMITTED for a run that had failed nine hours earlier and
# exited. A watch reports on the run it was started to watch.
FIRED_AFTER_UTC = datetime.datetime.utcnow()


LOG_TAIL_LINES = 25
LINE_CHARS = 300


def runs_coll():
    m = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")
    return m["pipelineAdmin"]["pipeline.runs"]


def log_coll():
    m = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")
    return m["pipelineAdmin"]["Log_dev"]


def _run_log_tail(run_id: str, limit: int = LOG_TAIL_LINES) -> list[str]:
    """The run's own log lines, newest last. Read only when the status API did
    not answer: the API is where status comes from, and the log is where the
    reason the API is silent comes from."""
    rows = list(log_coll().find(
        {"run_id": run_id},
        {"timestamp": 1, "asctime": 1, "level": 1, "levelname": 1,
         "component": 1, "step": 1, "message": 1, "msg": 1}
    ).sort([("_id", -1)]).limit(limit))
    if not rows:
        # A run whose controller never started stamps no run_id, so fall back
        # to the newest lines from any component and say so.
        rows = list(log_coll().find(
            {}, {"timestamp": 1, "asctime": 1, "level": 1, "levelname": 1,
                 "component": 1, "step": 1, "message": 1, "msg": 1}
        ).sort([("_id", -1)]).limit(limit))
        if rows:
            rows[-1] = dict(rows[-1])
            rows[-1]["message"] = (
                "(no line carries this run_id -- newest lines from any "
                "component follow) " + str(rows[-1].get("message")
                                            or rows[-1].get("msg") or ""))
    out = []
    for r in reversed(rows):
        when = r.get("timestamp") or r.get("asctime") or "?"
        lvl = r.get("level") or r.get("levelname") or "?"
        who = r.get("component") or "?"
        step = f"[{r['step']}]" if r.get("step") else ""
        text = str(r.get("message") or r.get("msg") or "")
        # One log line carries the whole work-item payload -- the entire
        # pipeline config, thousands of characters. Unbounded, it buries the
        # status file it was meant to explain.
        if len(text) > LINE_CHARS:
            text = text[:LINE_CHARS] + f" ...[{len(text)} chars, truncated]"
        out.append(f"    {when} {lvl} {who}{step} {text}")
    return out


def feed_status_log(run_id: str | None, note: str) -> None:
    """One visible block per tick, APPENDED to the log.

    Per-step status comes from the run-status API; the admission header
    (status/heartbeat/vm/phase) is read from the run record -- the only thing
    that exists before the controller's API answers.

    When the API call FAILS, the whole failure is written verbatim (never
    truncated) and the run's own log tail is written beneath it, so the file
    says both that the call failed and what the run was doing when it did
    (operator ruling 2026-10-24).
    """
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n===== tick {datetime.datetime.utcnow().isoformat()}Z "
                f"{note} =====\n")
        if run_id:
            try:
                r = runs_coll().find_one({"run_id": run_id}) or {}
                hb = r.get("controller_heartbeat_at")
                age = (f"{int((datetime.datetime.utcnow() - hb).total_seconds())}s ago"
                       if hb else "none yet")
                f.write(f"  run={run_id} status={r.get('status')} "
                        f"start_at={r.get('start_at_step')}\n"
                        f"  admitted={r.get('started_at')} "
                        f"controller_heartbeat={age} "
                        f"vm={r.get('vm_name') or 'not provisioned yet'} "
                        f"phase={r.get('controller_phase') or '-'}\n")
            except Exception as exc:  # noqa: BLE001
                f.write(f"  (run record unreadable: {type(exc).__name__}: {exc})\n")
            f.flush()
            proc = subprocess.run(
                [sys.executable,
                 "pipeline/run_lifecycle/ops/checkPipelineStatus.py", run_id],
                capture_output=True, text=True, timeout=240, check=False)
            body = (proc.stdout or "") + (proc.stderr or "")
            f.write(body if body.endswith("\n") else body + "\n")
            failed = proc.returncode != 0 or "unreachable" in body.lower()
            if failed:
                # Say the same words every five minutes so the file reads as a
                # heartbeat rather than a string of errors: the API is served
                # by the controller, so no answer before the controller is up
                # is the normal case, not a fault.
                waiting_on = ("the controller is not up yet"
                              if not (r or {}).get("controller_heartbeat_at")
                              else "the controller is up but not answering")
                f.write(f"  WAITING FOR THE STATUS API -- {waiting_on} "
                        f"(exit={proc.returncode}); checking again in 5 minutes\n")
                f.write("  --- the run's log, newest last ---\n")
                try:
                    lines = _run_log_tail(run_id)
                    f.write("\n".join(lines) + "\n" if lines
                            else "    (the log holds no lines yet)\n")
                except Exception as exc:  # noqa: BLE001
                    f.write(f"    (log unreadable: {type(exc).__name__}: {exc})\n")
        f.flush()


def _observe(run_id: str, last_status):
    """One reading of a known run: report a change, and on a terminal
    status write the final report and answer the exit code. Returns
    (status, exit_code-or-None).

    Lifted out of main() for the same reason as _await_admission: an
    entry function drives and does no more than twenty-five things."""
    r = runs_coll().find_one({"run_id": run_id})
    status = (r or {}).get("status", "?")
    if status != last_status:
        _log.LogPipeline("INFO", "STATUS-CHANGE run_id=%s %s -> %s",
                         run_id, last_status, status)
    if status in ("succeeded", "failed", "aborted", "error"):
        feed_status_log(run_id, f"TERMINAL {status}")
        _log.LogPipeline("INFO", "TERMINAL run_id=%s status=%s",
                         run_id, status)
        return status, (0 if status == "succeeded" else 1)
    return status, None


def _await_admission():
    """The newest run admitted since this watch began, or None.

    Lifted out of main(): an entry function drives and does no more than
    twenty-five things, and main() did twenty-eight."""
    r = runs_coll().find_one(
        {"pipeline_name": "medicare",
         "_id": {"$gte": ObjectId.from_datetime(FIRED_AFTER_UTC)}},
        sort=[("_id", -1)])
    if not r:
        return None
    _log.LogPipeline("INFO", "ADMITTED run_id=%s status=%s",
                     r["run_id"], r.get("status"))
    return r["run_id"], r.get("status")


def _destination(argv=None) -> str:
    """Where the status report is written. The only argument."""
    import argparse  # noqa: PLC0415
    parser = argparse.ArgumentParser(
        description="Watch the run and write its status report.")
    parser.add_argument("destination", nargs="?", default=_DEFAULT_LOG,
                        help="file the status report is appended to; "
                             "defaults to " + _DEFAULT_LOG)
    return parser.parse_args(argv).destination


def main() -> int:
    global LOG
    LOG = _destination()
    run_id = None
    last_status = None
    last_feed = 0.0
    while True:
        try:
            if run_id is None:
                # Discovery is by the record's own insertion time, carried by
                # the ObjectId, because started_at is not written until the
                # controller is up -- a filter on it cannot see a run that was
                # admitted seconds ago. No start_at_step filter: the step the
                # DAG begins at is the operator's choice per fire, not a
                # property the watcher may assume.
                found = _await_admission()
                if found:
                    run_id, last_status = found
            else:
                last_status, verdict = _observe(run_id, last_status)
                if verdict is not None:
                    return verdict
            if time.time() - last_feed >= 300:
                feed_status_log(run_id, f"run_id={run_id or 'awaiting admission'}")
                last_feed = time.time()
        except Exception as exc:  # noqa: BLE001
            _log.LogPipeline("ERROR", "WATCHER-ERROR %s: %s",
                             type(exc).__name__, str(exc)[:200])
            time.sleep(60)
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
