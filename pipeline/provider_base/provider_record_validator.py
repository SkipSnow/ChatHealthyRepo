# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

REQUIRED_TOP = ("npi",)

# active.is_active values that mean the provider is currently active. The
# other two ("original_false", "history_false") mean deactivated. A
# deactivated NPPES row carries only NPI + deactivation date, so an inactive
# provider may legitimately lack entity_type_code; an active one may not.
_ACTIVE_STANDING = ("Default_true", "history_true")


def validate_provider_record(doc: dict) -> tuple[bool, list[str]]:
    reasons = []
    for field in REQUIRED_TOP:
        if not doc.get(field):
            reasons.append(f"missing_{field}")
    npi = str(doc.get("npi", ""))
    if npi and (len(npi) != 10 or not npi.isdigit()):
        reasons.append("invalid_npi")
    etc = str(doc.get("entity_type_code") or "")
    if etc and etc not in ("1", "2"):
        reasons.append("invalid_entity_type_code")
    is_active = (doc.get("active") or {}).get("is_active")
    if not etc and is_active in _ACTIVE_STANDING:
        reasons.append("missing_entity_type_code")
    return (len(reasons) == 0, reasons)
