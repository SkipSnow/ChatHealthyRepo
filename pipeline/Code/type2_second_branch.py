# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from pymongo import UpdateOne

from pipeline_runtime import PipelineRuntime
from specialty_classification_catalog import load_catalog

_BULK_WRITE_CHUNK = 1000


def enrich_type2_second(ctx) -> dict:
    catalog = ctx.catalog or load_catalog()
    rt = PipelineRuntime(ctx)
    part = ctx.config.get("partition") or {}
    state = part.get("business_address_state", "")
    filt = {"entity_type_code": "2"}
    if state:
        filt = {**rt.partition_filter(state), "entity_type_code": "2"}
    updated = 0

    update_ops: list[UpdateOne] = []

    def _flush() -> None:
        if not update_ops:
            return
        rt.providers_coll.bulk_write(update_ops, ordered=False)
        update_ops.clear()

    for doc in rt.providers_coll.find(filt).batch_size(_BULK_WRITE_CHUNK):
        descriptions = []
        for tax in doc.get("taxonomies") or []:
            code = tax.get("code")
            entry = catalog.get(code or "")
            if entry:
                descriptions.append({"code": code, "classification_label": "facility"})
        if descriptions:
            update_ops.append(UpdateOne(
                {"_id": doc["_id"]},
                {"$set": {"facility_type_descriptions": descriptions}},
            ))
            updated += 1
            if len(update_ops) >= _BULK_WRITE_CHUNK:
                _flush()

    _flush()
    return {"updated": updated, "state": state or "ALL"}
