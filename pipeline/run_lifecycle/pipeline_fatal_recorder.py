# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""record_fatal_discrepancy: single sink for pipeline fatals.

Every raise site inside PipelineDatasetRegistry and GenericPipelineExecutor
calls this helper BEFORE re-raising the ChatHealthyException. That gives
the discrepancy report a complete picture of every fatal seen during the
run, including the ones that abend the pipeline before the report step
would otherwise fire.

The write is best-effort. If Mongo is unavailable or the write itself
raises, we log via ChatHealthyLoggingService and swallow the secondary
failure so the ORIGINAL ChatHealthyException re-raise is never blocked by
the recorder.

All pipeline metadata lives in the `Pipelines` database
(operator directive 2026-08-03: pipeline coordination metadata lives on
the pipeline cluster, never on the frontend cluster).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

_log = ChatHealthyLoggingService()

# The unified discrepancy store the report reads. A job-level fatal is a
# type_aggregate keyed by run under class fatal_<mode> (LLD v54 §7.5). The
# discrepancy report reads the aggregates, so a fatal recorded here appears in
# the report even when the run abends before the report step would fire.
FATAL_DISCREPANCIES_DB = "pipelineAdmin"
FATAL_DISCREPANCIES_COLL = "discrepancyLog"


# A process that cannot name or reach its log database has no way to record
# what it did. It is not allowed to continue and call the run a success.
LOG_DB_FATAL_MODES = frozenset({"log_db_not_configured", "log_db_unwritable"})


def is_log_db_fatal(exc: BaseException) -> bool:
    """True when exc is the logging substrate refusing to start."""
    return getattr(exc, "mode", None) in LOG_DB_FATAL_MODES


def _frontend_mongo():
    """The front-end cluster handle the fatal marker is written through.

    Derived here rather than taken from the caller: callers hand in whichever
    client they hold, and several pass the pipeline cluster, where the
    discrepancy store the report reads does not live. Kept as a seam so a test
    can redirect it to a scratch cluster.
    """
    return ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyFrontEnd")


def record_fatal_discrepancy(
    pipeline_mongo,
    *,
    run_id: Optional[str],
    step: str,
    exc: ChatHealthyException,
) -> None:
    """Best-effort record of a job-level fatal into discrepancyLog. Never raises.

    Written as a type_aggregate under class fatal_<mode>, artifact 'run', so
    the discrepancy report renders it as the run's single fatal.
    """
    # A domain-class fatal is already recorded in discrepancyLog by
    # write_finding; recording a second job-level marker for the same event
    # would give the run two fatals. Skip it so a run records exactly one.
    if getattr(exc, "context", {}).get("already_recorded_fatal"):
        return
    # No early return on a null pipeline_mongo: the write derives its own
    # front-end client and does not use this parameter, so returning here would
    # suppress the one record that says why a run died on the callers most
    # likely to hold nothing -- the ones failing.
    cls = f"fatal_{exc.mode}"
    now = datetime.now(timezone.utc).isoformat()
    try:
        _frontend_mongo()[FATAL_DISCREPANCIES_DB][FATAL_DISCREPANCIES_COLL].update_one(
            {"_id": f"{run_id}:run:type:{cls}"},
            {
                "$setOnInsert": {
                    "kind": "type_aggregate",
                    "run_id": run_id,
                    "artifact": "run",
                    "class": cls,
                    "recorded_at": now,
                },
                "$set": {
                    "severity": "fatal",
                    "step": step,
                    "explanation": str(exc),
                    "context": {
                        "mode": exc.mode,
                        "message": str(exc),
                        "fields": getattr(exc, "context", {}) or {},
                    },
                },
                "$inc": {"count": 1},
            },
            upsert=True,
        )
    except Exception as sec_exc:  # noqa: BLE001 - secondary failure MUST NOT mask the primary
        _log.LogPipeline("WARNING", 
            "record_fatal_discrepancy: could not persist fatal marker for "
            f"step={step!r} mode={exc.mode!r}: {sec_exc}"
        )
