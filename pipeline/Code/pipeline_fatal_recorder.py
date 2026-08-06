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

from chathealthy_frontend_lib.exceptions import ChatHealthyException
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

_log = ChatHealthyLoggingService()

FATAL_DISCREPANCIES_DB = "Pipelines"
FATAL_DISCREPANCIES_COLL = "pipeline.discrepancies"


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
    if pipeline_mongo is None:
        return
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
        coll = pipeline_mongo[FATAL_DISCREPANCIES_DB][FATAL_DISCREPANCIES_COLL]
        coll.insert_one(doc)
    except Exception as sec_exc:  # noqa: BLE001 - secondary failure MUST NOT mask the primary
        _log.warning(
            "record_fatal_discrepancy: could not persist fatal marker for "
            f"step={step!r} mode={exc.mode!r}: {sec_exc}"
        )
