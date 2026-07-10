# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from type1_second_branch import enrich_type1_second

def run_step(ctx):
    return enrich_type1_second(ctx)


def execute(ctx):
    return run_step(ctx)
