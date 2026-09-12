# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from pipeline_runtime import PipelineRuntime
from schemas.provider_record_validator import validate_provider_record


def reconcile_npi_sets(expected: set[str], actual: set[str]) -> dict[str, list[str]]:
    return {
        "missing": sorted(expected - actual),
        "extra": sorted(actual - expected),
    }


def reconcile_post_load(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    part = ctx.config.get("partition") or {}
    state = part.get("business_address_state", "")
    filt = rt.partition_filter(state) if state else {}
    valid = invalid = 0
    for doc in rt.providers_coll.find(filt):
        ok, reasons = validate_provider_record(doc)
        if ok:
            valid += 1
        else:
            invalid += 1
            for reason in reasons:
                rt.record_discrepancy(
                    npi=doc.get("npi"), reason=reason, step="post_load_reconciliation",
                    state=state or rt.mailing_state(doc), entity_kind=rt.entity_kind(doc),
                )
    return {"valid": valid, "invalid": invalid, "state": state or "ALL"}
