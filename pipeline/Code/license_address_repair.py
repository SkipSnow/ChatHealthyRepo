# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

import logging

from pipeline_runtime import PipelineRuntime

_log = logging.getLogger("license_address_repair")


def repair_license_addresses(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    part = ctx.config.get("partition") or {}
    state = part.get("business_address_state", "")
    filt = rt.partition_filter(state) if state else {}
    repaired = flagged = 0
    for doc in rt.providers_coll.find(filt):
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
            rt.providers_coll.update_one({"_id": doc["_id"]}, {"$set": {"addresses": doc.get("addresses")}})
    return {"repaired": repaired, "flagged": flagged, "state": state or "ALL"}
