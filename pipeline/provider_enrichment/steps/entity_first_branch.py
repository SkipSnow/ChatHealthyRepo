# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""First entity branch — one worker per state, Type 2 (institutional) only.

Individuals carry their self-reported sex as provider_sex_code from the
source row, so there is no Type 1 work in this branch.
"""

from __future__ import annotations

from pipeline.provider_enrichment.type2_first_branch import enrich_type2_first


def run_step(ctx):
    return enrich_type2_first(ctx)


def execute(ctx):
    return run_step(ctx)
