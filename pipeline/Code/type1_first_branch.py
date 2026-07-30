# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from pymongo import UpdateOne

from pipeline_runtime import PipelineRuntime

_BULK_WRITE_CHUNK = 1000


def enrich_type1_first(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    part = ctx.config.get("partition") or {}
    state = part.get("business_address_state", "")
    filt = {"entity_type_code": "1"}
    if state:
        filt = {**rt.partition_filter(state), "entity_type_code": "1"}
    updated = 0

    update_ops: list[UpdateOne] = []

    def _flush() -> None:
        if not update_ops:
            return
        rt.providers_coll.bulk_write(update_ops, ordered=False)
        update_ops.clear()

    for doc in rt.providers_coll.find(filt).batch_size(_BULK_WRITE_CHUNK):
        patch = {}
        if "sex" not in doc and doc.get("provider_sex_code"):
            patch["sex"] = doc["provider_sex_code"]
        if patch:
            update_ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": patch}))
            updated += 1
            if len(update_ops) >= _BULK_WRITE_CHUNK:
                _flush()

    _flush()
    return {"updated": updated, "state": state or "ALL"}
