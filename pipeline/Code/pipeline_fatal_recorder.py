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

from datetime import datetime
from typing import Optional

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

_log = ChatHealthyLoggingService()



# A process that cannot name or reach its log database has no way to record
# what it did. It is not allowed to continue and call the run a success.
LOG_DB_FATAL_MODES = frozenset({"log_db_not_configured", "log_db_unwritable"})


def is_log_db_fatal(exc: BaseException) -> bool:
    """True when exc is the logging substrate refusing to start."""
    return getattr(exc, "mode", None) in LOG_DB_FATAL_MODES


def record_fatal_discrepancy(
    pipeline_mongo,
    *,
    run_id: Optional[str],
    step: str,
    exc: ChatHealthyException,
) -> None:
    """Best-effort insert of a fatal-shaped discrepancy row. Never raises.

    Row layout matches the pipeline.discrepancies contract (run_id / npi /
    reason / step / entity_kind / context) so the discrepancy report
    renders fatals in the same table as data discrepancies, distinguishable
    by entity_kind='pipeline_fatal' and reason='fatal_<mode>'.
    """
    # No early return on a null pipeline_mongo. The write below derives its own
    # front-end client and does not use this parameter at all, so returning
    # here suppressed the one record that says why a run died -- on the exact
    # callers most likely to hold nothing, which are the ones failing.
    now = datetime.utcnow()
    doc = {
        "run_id": run_id,
        "reason": f"fatal_{exc.mode}",
        "step": step,
        "entity_kind": "pipeline_fatal",
        "npi": None,
        "context": {
            "mode": exc.mode,
            "message": str(exc),
            "fields": getattr(exc, "context", {}) or {},
        },
        "recorded_at": now,
        "created_at": now,
    }
    try:
        # pipelineAdmin is on the FRONT END, and callers hand in whichever
        # client they happen to hold -- PipelineRuntime and the county cascade
        # pass the pipeline one. A fatal marker written through that client
        # landed in a manufactured pipelineAdmin nothing reads, so the one
        # record explaining why a run died was the record that went missing.
        # db_call carries the write verbatim. It is the same characters as the
        # line below it, so a program can read both and fail the build when
        # they diverge -- which a comment cannot do, and which is why the four
        # comments asserting "coordination data lives on the pipeline cluster"
        # sat next to code correctly reaching the front end for months.
        _log.debug(
            "record_fatal_discrepancy write",
            db_call='getConnection("pipelineEditor", "ChatHealthyFrontEnd")'
                    '["pipelineAdmin"]["pipeline.discrepancies"].insert_one(doc)',
            cluster="ChatHealthyFrontEnd",
            database="pipelineAdmin",
            collection="pipeline.discrepancies",
            operation="insert_one",
        )
        ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")["pipelineAdmin"]["pipeline.discrepancies"].insert_one(doc)
    except Exception as sec_exc:  # noqa: BLE001 - secondary failure MUST NOT mask the primary
        _log.warning(
            "record_fatal_discrepancy: could not persist fatal marker for "
            f"step={step!r} mode={exc.mode!r}: {sec_exc}"
        )
