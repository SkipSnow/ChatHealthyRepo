# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from pipeline_runtime import PipelineRuntime
from specialty_classification_catalog import load_catalog


def enrich_type2_second(ctx) -> dict:
    catalog = ctx.catalog or load_catalog()
    rt = PipelineRuntime(ctx)
    part = ctx.config.get("partition") or {}
    state = part.get("business_address_state", "")
    filt = {"entity_type_code": "2"}
    if state:
        filt = {**rt.partition_filter(state), "entity_type_code": "2"}
    updated = 0
    for doc in rt.providers_coll.find(filt):
        descriptions = []
        for tax in doc.get("taxonomies") or []:
            code = tax.get("code")
            entry = catalog.get(code or "")
            if entry:
                descriptions.append({"code": code, "classification_label": "facility"})
        if descriptions:
            rt.providers_coll.update_one({"_id": doc["_id"]}, {"$set": {"facility_type_descriptions": descriptions}})
            updated += 1
    return {"updated": updated, "state": state or "ALL"}
