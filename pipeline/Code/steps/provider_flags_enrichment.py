# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from provider_flags_enrichment import apply_provider_flags_fn


def run_step(ctx) -> dict:
    config = dict(ctx.config)
    config["states"] = ctx.args.resolved_states()
    config["provider_collection"] = ctx.provider_collection
    return apply_provider_flags_fn(config)


def execute(ctx):
    return run_step(ctx)
