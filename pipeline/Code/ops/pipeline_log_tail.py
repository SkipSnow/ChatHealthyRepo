# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Tail pipelineAdmin.Log to the terminal.

The pipeline's components log to Mongo as they work. Azure Automation buffers
a runbook's own stream until the job ends, so the Mongo log is the only place
a run can be watched while it is still going.

  python pipeline/Code/ops/pipeline_log_tail.py
  python pipeline/Code/ops/pipeline_log_tail.py --minutes 30 --once
  python pipeline/Code/ops/pipeline_log_tail.py --contains chregtest
  python pipeline/Code/ops/pipeline_log_tail.py --component reservation-reaper
"""
import argparse
import os
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone

_HERE = pathlib.Path(__file__).resolve()
for _parent in _HERE.parents:
    _lib = _parent / "ChatHealthyLib" / "src"
    if _lib.is_dir():
        sys.path.insert(0, str(_lib))
        _ENV_FILE = _parent / ".env"
        break
else:
    _ENV_FILE = None

if _ENV_FILE is not None and _ENV_FILE.is_file():
    for _line in _ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in _line and not _line.strip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

os.environ.setdefault("CH_SPACE_NAME", "pipeline-log-tail")
os.environ.setdefault("CH_COMPONENT", "pipeline-log-tail")
os.environ.setdefault("CH_LOG_DESTINATION", "stderr")

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

log = ChatHealthyLoggingService()

ADMIN_DB = "pipelineAdmin"
LOG_COLLECTION = "Log"


def _raise_no_log(db_name: str, collection: str) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="config_error",
        component="pipeline_log_tail",
        message=f"{db_name}.{collection} is not present on this cluster")


class PipelineLogTail:
    """Reads the pipeline's Mongo log forward from a point in time."""

    def __init__(self, identity: str = "pipelineEditor") -> None:
        client = ChatHealthyMongoUtilities().getConnection(identity, "ChatHealthyFrontEnd")
        self._db = client["pipelineAdmin"]
        if LOG_COLLECTION not in self._db.list_collection_names():
            _raise_no_log(ADMIN_DB, LOG_COLLECTION)
        self._collection = self._db[LOG_COLLECTION]
        self._seen = set()

    def since(self, minutes: int) -> datetime:
        return datetime.now(timezone.utc) - timedelta(minutes=minutes)

    def rows(self, after: datetime) -> list:
        """Every row at or after `after` this call has not already returned.

        Rows are matched on _id rather than only on time: several components
        write inside the same second, and advancing a timestamp cursor past
        them would drop the ones that arrived late.
        """
        fresh = []
        for row in self._collection.find({"timeStamp": {"$gte": after}}).sort("timeStamp", 1):
            key = str(row.get("_id"))
            if key in self._seen:
                continue
            self._seen.add(key)
            fresh.append(row)
        return fresh

    @staticmethod
    def wanted(row: dict, component: str, contains: str) -> bool:
        if component and component not in str(row.get("component") or ""):
            return False
        if contains and contains not in str(row.get("formatted") or row.get("message") or ""):
            return False
        return True

    @staticmethod
    def render(row: dict) -> str:
        stamp = str(row.get("timeStamp"))[11:19]
        who = str(row.get("component") or "")[:18].ljust(18)
        body = str(row.get("formatted") or row.get("message") or "")
        marker = " " if str(row.get("level") or "").upper() != "ERROR" else "!"
        return f"{stamp}{marker}{who} {body}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Tail the pipeline's Mongo log.")
    parser.add_argument("--minutes", type=int, default=15,
                        help="how far back to start (default 15)")
    parser.add_argument("--interval", type=int, default=10,
                        help="seconds between polls (default 10)")
    parser.add_argument("--component", default="",
                        help="only rows whose component contains this")
    parser.add_argument("--contains", default="",
                        help="only rows whose message contains this")
    parser.add_argument("--once", action="store_true",
                        help="print what is there and exit")
    parser.add_argument("--identity", default="pipelineEditor",
                        help="Mongo identity to read as")
    args = parser.parse_args()

    tail = PipelineLogTail(args.identity)
    after = tail.since(args.minutes)
    log.info("tailing %s.%s from %s", ADMIN_DB, LOG_COLLECTION,
             after.strftime("%H:%M:%S"))

    quiet_polls = 0
    while True:
        rows = [r for r in tail.rows(after)
                if tail.wanted(r, args.component, args.contains)]
        for row in rows:
            log.info("%s", tail.render(row))
        if args.once:
            return 0
        quiet_polls = 0 if rows else quiet_polls + 1
        if quiet_polls and quiet_polls % 30 == 0:
            log.info("... quiet for %d polls", quiet_polls)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
