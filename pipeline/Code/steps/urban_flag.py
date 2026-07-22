# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v23 §4.14 Urban flag per address."""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


from urban_flag_engine import stamp_urban_flags

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    config = dict(ctx.config)
    config["states"] = ctx.args.resolved_states()
    if getattr(ctx, "provider_collection", None):
        config["provider_collection"] = ctx.provider_collection
    return stamp_urban_flags(config, mongo=ctx.mongo_client, blob=ctx.blob_client) or {}


def execute(ctx):
    return run_step(ctx)
