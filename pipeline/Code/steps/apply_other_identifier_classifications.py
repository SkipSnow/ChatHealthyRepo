# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""apply_other_identifier_classifications — Provider Pipeline step wrapper.

Bridges StepContext to
other_identifier_phrases_engine.apply_other_identifier_classifications.

Per-state fanout. Each worker walks provider records for its
business_address_state, reads the OtherIdentifierPhrases staging table,
and moves each other_identifier entry into insurance[] / licenses[] /
retained other_identifiers[] with a classification tag.
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

from other_identifier_phrases_engine import apply_other_identifier_classifications

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    config = dict(ctx.config)
    config.setdefault("run_id", ctx.run_id)
    config.setdefault("provider_collection", ctx.provider_collection)
    partition = getattr(ctx, "partition", None) or {}
    config["partition"] = partition

    result = apply_other_identifier_classifications(
        config,
        mongo=ctx.mongo_client,
        blob=ctx.blob_client,
    ) or {}

    state_key = (partition.get("business_address_state") or "UNKNOWN").upper()
    _log.info("apply_other_identifier_classifications state=%s summary: %s",
              state_key, result)
    ctx.manifest.metrics.setdefault(
        "apply_other_identifier_classifications", {}
    )[state_key] = result
    return result


def execute(ctx):
    return run_step(ctx)
