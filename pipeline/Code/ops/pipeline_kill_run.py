# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Stop a pipeline run the way it is built to be stopped.

SIGINT, to the Controller, and nothing else.

Python's default SIGINT handler raises KeyboardInterrupt, which unwinds the
stack and runs finally blocks. SIGTERM has no such handler: the process dies
where it stands. That single difference is why `kill` and `az vm delete` both
destroy a run, and `kill -2` ends it.

KeyboardInterrupt derives from BaseException, so it passes through the
Controller's `except ChatHealthyException` and `except Exception` untouched
and its finally block runs. That block already does everything wanted:

    _fatal_on_worker_log_db_reports()   records why, while the evidence exists
    _kill_active_workers()              SIGTERMs every worker it spawned
    _quiesce_mongo_state()              cancels the reservation, marks the
                                        manifest, emits the discrepancy report
    _pause_pipeline_cluster()           pauses Atlas
    _fire_farewell_vm_delete()          deletes its own host

So the Controller kills its own workers, writes its own report and removes its
own machine. Nothing here does any of that: signalling it is the whole job.

    python pipeline/Code/ops/pipeline_kill_run.py                 # newest run
    python pipeline/Code/ops/pipeline_kill_run.py --run-id RUN
    python pipeline/Code/ops/pipeline_kill_run.py --dry-run

Exit: 0 the signal was delivered, 1 it was not, 2 there was nothing to stop.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

_HERE = pathlib.Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / "pipeline" / "Code").is_dir():
        sys.path.insert(0, str(_parent / "pipeline" / "Code"))
        sys.path.insert(0, str(_parent / "ChatHealthyLib" / "src"))
        _ROOT = _parent
        break

_ENV = _ROOT / ".env"
if _ENV.is_file():
    for _line in _ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in _line and not _line.strip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

os.environ.setdefault("CH_SPACE_NAME", "pipeline-kill-run")
os.environ.setdefault("CH_COMPONENT", "pipeline-kill-run")
os.environ["CH_LOG_DESTINATION"] = "stdout"

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

log = ChatHealthyLoggingService()

ADMIN_DB = "pipelineAdmin"
TERMINAL = ("succeeded", "failed", "aborted", "completed")


def _raise_no_controller_pid(run_id: str) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="controller_pid_absent",
        component="pipeline_kill_run",
        message=(f"{run_id} records no controller_pid, so the Controller "
                 f"cannot be signalled. The pid is written by the heartbeat "
                 f"on the reservation and on the run; a run without one "
                 f"predates that or never started its Controller."))


def _az(args: list[str]) -> tuple[int, str]:
    r = subprocess.run(["az"] + args, capture_output=True, text=True, shell=True)
    return r.returncode, (r.stdout or r.stderr or "").strip()


def _run_shell(resource_group: str, vm: str, script: str) -> str:
    """One command on the host, through Run Command."""
    code, out = _az(["vm", "run-command", "invoke", "--resource-group",
                     resource_group, "--name", vm, "--command-id",
                     "RunShellScript", "--scripts", script, "-o", "json"])
    if code != 0:
        return f"__error__ {out[:300]}"
    try:
        return "\n".join(v.get("message", "") for v in json.loads(out).get("value", []))
    except ValueError:
        return out


def find_run(db, run_id: str | None):
    if run_id:
        return db["pipeline.runs"].find_one({"run_id": run_id})
    return db["pipeline.runs"].find_one(
        {"status": {"$nin": list(TERMINAL)}}, sort=[("started_at", -1)])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stop a pipeline run by signalling its Controller.")
    parser.add_argument("--run-id", default=None,
                        help="run to stop (default: newest non-terminal run)")
    parser.add_argument("--resource-group",
                        default=os.environ.get("AUTOMATION_RESOURCE_GROUP",
                                               "rg-chathealthy-pipeline-dev"))
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be signalled and stop")
    parser.add_argument("--wait", type=int, default=300,
                        help="seconds to watch for the run to go terminal")
    args = parser.parse_args()

    db = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyFrontEnd")[ADMIN_DB]
    run = find_run(db, args.run_id)
    if not run:
        log.info("nothing to stop: no non-terminal run")
        return 2

    run_id = run.get("run_id")
    status = str(run.get("status", "")).lower()
    if status in TERMINAL:
        log.info("%s is already %s; nothing to stop", run_id, status)
        return 2

    reservation = db["cluster_lifecycle"].find_one({"_id": run_id}) or {}
    vm = reservation.get("vm_name") or run.get("vm_name")
    pid = reservation.get("controller_pid") or run.get("controller_pid")
    log.info("run=%s status=%s host=%s controller_pid=%s",
             run_id, status, vm, pid)

    if not vm:
        log.info("%s names no host; nothing to signal", run_id)
        return 1
    if not pid:
        _raise_no_controller_pid(run_id)

    if args.dry_run:
        log.info("dry run: would send SIGINT to pid %s on %s", pid, vm)
        return 0

    # SIGINT, not SIGTERM. The Controller's finally block does the rest --
    # workers, report, reservation, cluster, host.
    out = _run_shell(args.resource_group, vm,
                     f"kill -2 {int(pid)} 2>&1 && echo SIGNAL_SENT "
                     f"|| echo SIGNAL_FAILED")
    log.info("host said: %s", out.replace("\n", " | ")[:300])
    if "SIGNAL_SENT" not in out:
        log.error("could not signal pid %s on %s", pid, vm)
        return 1

    log.info("SIGINT delivered to controller pid %s; waiting up to %ds for "
             "quiesce (it cancels the reservation, writes the discrepancy "
             "report, pauses the cluster and deletes its own host)",
             pid, args.wait)
    deadline = time.time() + args.wait
    while time.time() < deadline:
        current = db["pipeline.runs"].find_one({"run_id": run_id}) or {}
        now = str(current.get("status", "")).lower()
        waited = int(time.time() - (deadline - args.wait))
        if now in TERMINAL:
            log.info("%s reached %s after %ds", run_id, now, waited)
            return 0
        log.info("waiting on %s: status=%s (%ds/%ds)", run_id, now, waited,
                 args.wait)
        time.sleep(15)
    log.warning("%s had not reached a terminal state within %ds; the "
                "Controller may still be quiescing", run_id, args.wait)
    return 1


if __name__ == "__main__":
    sys.exit(main())
