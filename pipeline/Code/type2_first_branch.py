# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from pipeline_runtime import PipelineRuntime


def enrich_type2_first(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    part = ctx.config.get("partition") or {}
    state = part.get("business_address_state", "")
    filt = {"entity_type_code": "2"}
    if state:
        filt = {**rt.partition_filter(state), "entity_type_code": "2"}
    updated = flagged = 0
    for doc in rt.providers_coll.find(filt):
        for field in ("authorized_official", "parent_organization"):
            if doc.get(field) is None:
                rt.record_discrepancy(
                    npi=doc.get("npi"), reason=f"{field}_incomplete", step="type2_first_branch",
                    state=state or rt.mailing_state(doc), entity_kind=rt.entity_kind(doc),
                )
                flagged += 1
    return {"updated": updated, "flagged": flagged, "state": state or "ALL"}
