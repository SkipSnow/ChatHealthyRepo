# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

from county_cascade_engine import run_county_cascade


def execute(ctx) -> dict:
    part = ctx.config.get("partition") or {}
    return run_county_cascade(
        ctx,
        part.get("business_address_state", ""),
        part.get("kind", "primary_practice"),
    )
