# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from pipeline_runtime import PipelineRuntime


def enrich_type1_second(ctx) -> dict:
    """Individual second-branch pass — provider flags are stamped by provider_flags_enrichment."""
    rt = PipelineRuntime(ctx)
    part = ctx.config.get("partition") or {}
    state = part.get("business_address_state", "")
    filt = {"entity_type_code": "1"}
    if state:
        filt = {**rt.partition_filter(state), "entity_type_code": "1"}
    scanned = rt.providers_coll.count_documents(filt)
    return {"scanned": scanned, "state": state or "ALL"}
