# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""harvest_other_identifier_phrases — Provider Pipeline step wrapper.

Bridges StepContext to other_identifier_phrases_engine.harvest_*.
Runs serial across the whole target Provider collection; upserts
unique (type_code, issuer_text) tuples into
PublicStaging.OtherIdentifierPhrases.
"""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from other_identifier_phrases_engine import harvest_other_identifier_phrases

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    config = dict(ctx.config)
    config.setdefault("run_id", ctx.run_id)
    config.setdefault("provider_collection", ctx.provider_collection)
    # partition_states, not resolved_states: ["ALL"] survives as the whole
    # country so harvest scans providers outside the fifty-one too. Handing it
    # the explicit fifty-one made it skip every territory and military row,
    # which classify and apply then could not find.
    config.setdefault("states", list(ctx.args.partition_states() or []))

    result = harvest_other_identifier_phrases(
        config,
        mongo=ctx.mongo_client,
        blob=ctx.blob_client,
    ) or {}

    _log.info("harvest_other_identifier_phrases summary: %s", result)
    ctx.manifest.metrics["harvest_other_identifier_phrases"] = result
    return result


def execute(ctx):
    return run_step(ctx)
