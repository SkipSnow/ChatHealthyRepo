# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""provider_flags_enrichment — Provider Pipeline step wrapper for LLD v23 §4.15/§4.16.

Bridges StepContext to provider_flags_engine.apply_provider_flags().
Applies the four F-105 flags (can_prescribe, is_homeopathic,
is_disqualified, is_npi_registered) to every provider record in the run.
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


from provider_flags_engine import apply_provider_flags

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    config = dict(ctx.config)
    config.setdefault("run_id", ctx.run_id)
    config.setdefault("env", ctx.env_prefix)
    config.setdefault("provider_collection", ctx.provider_collection)

    partition = getattr(ctx, "partition", None) or {}
    config.setdefault("entity_kind_filter", partition.get("entity_kind"))
    config.setdefault("partition_state", partition.get("business_address_state"))

    result = apply_provider_flags(
        config,
        mongo=ctx.mongo_client,
        blob=ctx.blob_client,
    ) or {}

    key = f"flags:{config.get('entity_kind_filter') or 'ALL'}:{config.get('partition_state') or 'ALL'}"
    ctx.manifest.metrics.setdefault("provider_flags", {})[key] = result
    return result


def execute(ctx):
    return run_step(ctx)
