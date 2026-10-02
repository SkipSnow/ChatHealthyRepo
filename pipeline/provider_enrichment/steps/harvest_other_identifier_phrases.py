# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""harvest_other_identifier_phrases — Provider Pipeline step wrapper.

Bridges StepContext to other_identifier_phrases_engine.harvest_*.
Per-state fanout. Each worker scans its state's providers and upserts
unique (type_code, issuer_text, state) tuples into
PublicStaging.OtherIdentifierPhrases.
"""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.provider_enrichment.other_identifier_phrases_engine import harvest_other_identifier_phrases

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    config = dict(ctx.config)
    config.setdefault("run_id", ctx.run_id)
    config.setdefault("provider_collection", ctx.provider_collection)
    partition = ctx.config.get("partition") or {}
    config["partition"] = partition

    result = harvest_other_identifier_phrases(
        config,
        mongo=ctx.mongo_client,
        blob=ctx.blob_client,
    ) or {}

    state_key = (partition.get("business_address_state") or "UNKNOWN").upper()
    _log.LogPipeline("INFO", "harvest_other_identifier_phrases state=%s summary: %s",
              state_key, result)
    ctx.manifest.metrics.setdefault(
        "harvest_other_identifier_phrases", {}
    )[state_key] = result
    return result


def execute(ctx):
    return run_step(ctx)
