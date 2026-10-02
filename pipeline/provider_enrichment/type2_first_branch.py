# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime


def enrich_type2_first(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    part = ctx.config.get("partition") or {}
    state = part.get("business_address_state", "")
    filt = {"entity_type_code": "2", "authorized_official": None}
    if state:
        filt = {**rt.partition_filter(state), **filt}
    projection = {"_id": 0, "npi": 1, "business_address.state": 1,
                  "entity_type_code": 1}
    flagged = 0
    for doc in rt.providers_coll.find(filt, projection).batch_size(1000):
        rt.record_discrepancy(
            npi=doc.get("npi"), reason="authorized_official_incomplete",
            step="type2_first_branch",
            state=state or rt.mailing_state(doc), entity_kind=rt.entity_kind(doc),
        )
        flagged += 1
    return {"updated": 0, "flagged": flagged, "state": state or "ALL"}
