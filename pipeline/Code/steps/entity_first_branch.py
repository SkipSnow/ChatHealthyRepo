# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""First entity branch — one worker per (state, entity type).

LLD v45 §5.2.11 fans out across state AND entity type. Type 1 and Type 2 are
disjoint sets, so this step dispatches to the type-specific engine the
partition names rather than running the two as sequential steps.
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException

from type1_first_branch import enrich_type1_first
from type2_first_branch import enrich_type2_first


def run_step(ctx):
    entity_type = (ctx.config.get("partition") or {}).get("entity_type")
    if entity_type == 1:
        return enrich_type1_first(ctx)
    if entity_type == 2:
        return enrich_type2_first(ctx)
    raise ChatHealthyException(
        mode="value_error",
        message=(
            "entity_first_branch: partition must name entity_type 1 or 2, "
            f"got {entity_type!r}"
        ),
        partition=ctx.config.get("partition"),
    )


def execute(ctx):
    return run_step(ctx)
