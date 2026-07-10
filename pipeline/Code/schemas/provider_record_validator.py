# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

REQUIRED_TOP = ("npi", "entity_type_code")


def validate_provider_record(doc: dict) -> tuple[bool, list[str]]:
    reasons = []
    for field in REQUIRED_TOP:
        if not doc.get(field):
            reasons.append(f"missing_{field}")
    npi = str(doc.get("npi", ""))
    if npi and (len(npi) != 10 or not npi.isdigit()):
        reasons.append("invalid_npi")
    etc = str(doc.get("entity_type_code", ""))
    if etc and etc not in ("1", "2"):
        reasons.append("invalid_entity_type_code")
    return (len(reasons) == 0, reasons)
