# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Second entity branch — one worker per (state, entity type).

Same shape as entity_first_branch: LLD v45 §5.2.11 fans out across state AND
entity type, and the two entity types are disjoint sets that need not wait
for each other.
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException

from type1_second_branch import enrich_type1_second
from type2_second_branch import enrich_type2_second


def run_step(ctx):
    entity_type = (ctx.config.get("partition") or {}).get("entity_type")
    if entity_type == 1:
        return enrich_type1_second(ctx)
    if entity_type == 2:
        return enrich_type2_second(ctx)
    raise ChatHealthyException(
        mode="value_error",
        message=(
            "entity_second_branch: partition must name entity_type 1 or 2, "
            f"got {entity_type!r}"
        ),
        partition=ctx.config.get("partition"),
    )


def execute(ctx):
    return run_step(ctx)
