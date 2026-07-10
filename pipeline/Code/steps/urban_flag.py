# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from urban_flag_step import stamp_urban_flags

def run_step(ctx):
    return stamp_urban_flags(ctx)


def execute(ctx):
    return run_step(ctx)
