# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from specialty_classification_catalog import load_catalog

def run_step(ctx):
    ctx.catalog = load_catalog()
    return {'catalog_codes': len(ctx.catalog)}


def execute(ctx):
    return run_step(ctx)
