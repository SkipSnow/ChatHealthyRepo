# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

from staging_collections import load_all_staging_sources


def execute(ctx) -> dict:
    states = ctx.args.resolved_states()
    result = load_all_staging_sources(ctx, states)
    ctx.step_summaries["load_staging_parallel"] = result
    return result
