# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

from pymongo import UpdateOne

from pipeline_runtime import PipelineRuntime

_log = ChatHealthyLoggingService()

_BULK_WRITE_CHUNK = 1000


def repair_license_addresses(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    part = ctx.config.get("partition") or {}
    state = part.get("business_address_state", "")
    filt = rt.partition_filter(state) if state else {}
    repaired = flagged = 0

    update_ops: list[UpdateOne] = []

    def _flush() -> None:
        if not update_ops:
            return
        rt.providers_coll.bulk_write(update_ops, ordered=False)
        update_ops.clear()

    for doc in rt.providers_coll.find(filt).batch_size(_BULK_WRITE_CHUNK):
        licenses = doc.get("licenses") or []
        changed = False
        for addr in doc.get("addresses") or []:
            if addr.get("state"):
                continue
            if len(licenses) == 1:
                lic_state = licenses[0].get("state")
                if lic_state:
                    addr["state"] = lic_state
                    addr["address_repair_provenance"] = "license_state_match"
                    repaired += 1
                    changed = True
            elif len(licenses) == 0:
                rt.record_discrepancy(
                    npi=doc.get("npi"), reason="state_missing_no_license", step="license_address_repair",
                    state=state or rt.mailing_state(doc), entity_kind=rt.entity_kind(doc),
                )
                flagged += 1
            else:
                rt.record_discrepancy(
                    npi=doc.get("npi"), reason="state_missing_ambiguous_license", step="license_address_repair",
                    state=state or rt.mailing_state(doc), entity_kind=rt.entity_kind(doc),
                )
                flagged += 1
        if changed:
            update_ops.append(UpdateOne(
                {"_id": doc["_id"]},
                {"$set": {"addresses": doc.get("addresses")}},
            ))
            if len(update_ops) >= _BULK_WRITE_CHUNK:
                _flush()

    _flush()
    return {"repaired": repaired, "flagged": flagged, "state": state or "ALL"}
