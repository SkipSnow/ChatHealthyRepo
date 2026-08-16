# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""provider_flags_enrichment -- Provider Pipeline step wrapper.

Bridges StepContext to provider_flags_engine.apply_provider_flags().
Stamps can_prescribe (credential level-1 MD/DO OR catalog level-2),
and is_homeopathic (catalog) on every
provider record in the run partition. No fallbacks: engine raises on
any unresolvable path.
"""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService


from pipeline_runtime import PipelineRuntime
from provider_flags_engine import apply_provider_flags

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    config = dict(ctx.config)
    config.setdefault("run_id", ctx.run_id)
    config.setdefault("data_version", int(ctx.args.data_version))
    config.setdefault("provider_collection", ctx.provider_collection)

    partition = getattr(ctx, "partition", None) or {}
    config.setdefault("entity_kind_filter", partition.get("entity_kind"))
    config.setdefault("partition_state", partition.get("business_address_state"))

    # This wrapper declared a logger and never called it, so a worker that
    # entered the step and stopped looked the same as one that never started.
    _log.info("provider_flags_enrichment[%s]: step begin run_id=%s entity=%s",
              config.get("partition_state") or "ALL", ctx.run_id,
              config.get("entity_kind_filter") or "ALL")
    try:
        result = apply_provider_flags(
            config,
            mongo=ctx.mongo_client,
            blob=ctx.blob_client,
        ) or {}
    except Exception as exc:
        _log.error("provider_flags_enrichment: step FAILED run_id=%s state=%s "
                   "%s: %s", ctx.run_id, config.get("partition_state") or "ALL",
                   type(exc).__name__, exc)
        raise
    _log.info("provider_flags_enrichment: step done run_id=%s state=%s %s",
              ctx.run_id, config.get("partition_state") or "ALL", result)

    key = f"flags:{config.get('entity_kind_filter') or 'ALL'}:{config.get('partition_state') or 'ALL'}"
    ctx.manifest.metrics.setdefault("provider_flags", {})[key] = result
    return result


def execute(ctx):
    return run_step(ctx)
