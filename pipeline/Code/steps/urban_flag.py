# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""urban_flag -- Provider Pipeline step wrapper.

Bridges StepContext to urban_flag_engine.apply_urban_flags(). Stamps
practice_addresses[i].county.urban per address and the provider-level urban
marker, both from the RUCC county_enrichment resolved. LLD v47 5.2.15.
"""

from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline_runtime import PipelineRuntime
from urban_flag_engine import apply_urban_flags

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    partition = ctx.config.get("partition") or {}
    config = dict(ctx.config)
    config.setdefault("run_id", ctx.run_id)
    config.setdefault("provider_collection", ctx.provider_collection)
    config.setdefault("partition_state", partition.get("business_address_state"))

    state = config.get("partition_state") or "ALL"
    _log.info("urban_flag[%s]: step begin run_id=%s", state, ctx.run_id)
    try:
        result = apply_urban_flags(config, mongo=rt.mongo, blob=ctx.blob_client) or {}
    except Exception as exc:
        _log.error("urban_flag[%s]: step FAILED run_id=%s %s: %s",
                   state, ctx.run_id, type(exc).__name__, exc)
        raise
    _log.info("urban_flag[%s]: step done run_id=%s %s", state, ctx.run_id, result)
    ctx.manifest.metrics.setdefault("urban_flag", {})[state] = result
    return result
