# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""county_enrichment_cascade — Provider Pipeline step wrapper for LLD v23 §4.13.

Bridges StepContext to county_cascade_engine.run_county_cascade().
"""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService


from county_cascade_engine import run_county_cascade

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    config = dict(ctx.config)
    config.setdefault("run_id", ctx.run_id)
    config.setdefault("env", ctx.env_prefix)
    config.setdefault("provider_collection", ctx.provider_collection)
    config.setdefault("data_version", int(ctx.args.data_version))
    config.setdefault("google_maps_enabled", bool(ctx.args.google_maps_enabled))

    # county_partitions collapsed to per-state only (commit 7ee164f0);
    # partition.kind no longer exists in the manifest. NPI-atomic
    # ownership: one worker per primary-practice state, enriches every
    # eligible address on its providers.
    partition = ctx.config.get("partition") or {}
    config.setdefault("partition_state", partition.get("business_address_state"))

    result = run_county_cascade(
        config,
        mongo=ctx.mongo_client,
        blob=ctx.blob_client,
    ) or {}

    key = f"county_cascade:{config.get('partition_state') or 'ALL'}"
    ctx.manifest.metrics.setdefault("county_enrichment", {})[key] = result
    return result


def execute(ctx):
    return run_step(ctx)
