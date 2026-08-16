# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Watch a pipeline run and say, every interval, what is true of it.

Written to be read by whoever comes back to a run that failed while nobody
was watching. It records the run's status, the step rows, the host, whether
the controller process is alive, and the last log lines -- the set of facts
that had to be reconstructed by hand after the run of 2026-08-15, when a
controller died and left nothing behind.

    python pipeline/Code/ops/pipeline_run_watch.py [--minutes 15]
                                                   [--pipeline provider]

Each pass appends a block to the report file and exits when the run reaches
a terminal state.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

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

os.environ.setdefault("CH_SPACE_NAME", "pipeline-run-watch")
os.environ.setdefault("CH_COMPONENT", "pipeline-run-watch")
os.environ["CH_LOG_DESTINATION"] = "stdout"

from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

log = ChatHealthyLoggingService()

ADMIN_DB = "pipelineAdmin"
TERMINAL = ("succeeded", "failed", "aborted", "completed")


def _az(args: list[str]) -> str:
    r = subprocess.run(["az"] + args, capture_output=True, text=True, shell=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _hosts(resource_group: str) -> list[str]:
    import json
    raw = _az(["vm", "list", "--resource-group", resource_group, "-o", "json"])
    try:
        return [v["name"] for v in json.loads(raw or "[]")]
    except ValueError:
        return []


def _report(db, pipeline: str, resource_group: str) -> bool:
    """One pass. Returns True when the run has reached a terminal state."""
    run = db["pipeline.runs"].find_one(
        {"pipeline_name": pipeline}, sort=[("started_at", -1)])
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if not run:
        log.info("%s  no %s run recorded", stamp, pipeline)
        return False

    status = str(run.get("status", "")).lower()
    log.info("%s  run=%s status=%s vm=%s controller_pid=%s reason=%s",
             stamp, run.get("run_id"), status, run.get("vm_name"),
             run.get("controller_pid"), run.get("failure_reason"))

    steps = list(db["pipeline.run_steps"].find({"run_id": run.get("run_id")})) \
        if "pipeline.run_steps" in db.list_collection_names() else []
    log.info("%s  step rows recorded: %d", stamp, len(steps))

    hosts = _hosts(resource_group)
    log.info("%s  hosts in %s: %s", stamp, resource_group, hosts or "none")

    since = datetime.now(timezone.utc) - timedelta(minutes=20)
    recent = list(db["Log"].find({"timeStamp": {"$gte": since}})
                  .sort("timeStamp", -1).limit(6))
    if not recent:
        log.info("%s  NO LOG LINES in the last 20 minutes -- the run is silent",
                 stamp)
    for row in reversed(recent):
        log.info("%s    %s %s | %s", stamp, str(row.get("timeStamp"))[11:19],
                 str(row.get("component"))[:18],
                 str(row.get("formatted") or row.get("message"))[:150])

    if status in TERMINAL:
        log.info("%s  run reached %s -- watch ends", stamp, status)
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch a pipeline run.")
    parser.add_argument("--minutes", type=int, default=15,
                        help="minutes between passes (default 15)")
    parser.add_argument("--pipeline", default="provider")
    parser.add_argument("--resource-group",
                        default=os.environ.get("AUTOMATION_RESOURCE_GROUP",
                                               "rg-chathealthy-pipeline-dev"))
    args = parser.parse_args()

    db = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyFrontEnd")[ADMIN_DB]
    log.info("watching %s every %d minute(s)", args.pipeline, args.minutes)
    while True:
        if _report(db, args.pipeline, args.resource_group):
            return 0
        time.sleep(args.minutes * 60)


if __name__ == "__main__":
    sys.exit(main())
