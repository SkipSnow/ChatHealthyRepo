# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

from provider_normalize_engine import per_state_fanout


def execute(ctx) -> dict:
    part = ctx.config.get("partition") or {}
    return per_state_fanout(ctx, part.get("business_address_state", ""))
