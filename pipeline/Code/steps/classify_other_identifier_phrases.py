# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""classify_other_identifier_phrases — Provider Pipeline step wrapper.

Bridges StepContext to
other_identifier_phrases_engine.classify_other_identifier_phrases.

Per-state fanout. Each worker classifies phrases seen in its state's
providers by calling a PydanticAI Gemini agent. First-writer-wins on
any phrase — subsequent state workers skip already-classified phrases.
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

from other_identifier_phrases_engine import classify_other_identifier_phrases

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    config = dict(ctx.config)
    config.setdefault("run_id", ctx.run_id)
    partition = getattr(ctx, "partition", None) or {}
    config["partition"] = partition

    result = classify_other_identifier_phrases(
        config,
        mongo=ctx.mongo_client,
        blob=ctx.blob_client,
    ) or {}

    state_key = (partition.get("business_address_state") or "UNKNOWN").upper()
    _log.info("classify_other_identifier_phrases state=%s summary: %s",
              state_key, result)
    ctx.manifest.metrics.setdefault(
        "classify_other_identifier_phrases", {}
    )[state_key] = result
    return result


def execute(ctx):
    return run_step(ctx)
