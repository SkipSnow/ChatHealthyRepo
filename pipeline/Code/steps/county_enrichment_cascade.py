# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""county_enrichment_cascade — Provider Pipeline step wrapper for LLD v23 §4.13.

Bridges StepContext to county_cascade_engine.run_county_cascade().
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


from county_cascade_engine import run_county_cascade

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    config = dict(ctx.config)
    config.setdefault("run_id", ctx.run_id)
    config.setdefault("env", ctx.env_prefix)
    config.setdefault("provider_collection", ctx.provider_collection)
    config.setdefault("google_maps_enabled", bool(ctx.args.google_maps_enabled))

    partition = ctx.config.get("partition") or {}
    config.setdefault("partition_state", partition.get("business_address_state"))
    config.setdefault("partition_kind", partition.get("kind"))

    result = run_county_cascade(
        config,
        mongo=ctx.mongo_client,
        blob=ctx.blob_client,
    ) or {}

    key = f"county_cascade:{config.get('partition_state') or 'ALL'}:{config.get('partition_kind') or 'BOTH'}"
    ctx.manifest.metrics.setdefault("county_enrichment", {})[key] = result
    return result


def execute(ctx):
    return run_step(ctx)
