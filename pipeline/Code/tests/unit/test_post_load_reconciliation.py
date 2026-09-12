# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from schemas.provider_record_validator import validate_provider_record
def test_validator():
    ok, reasons = validate_provider_record({'npi': '1234567890', 'entity_type_code': '1'})
    assert ok and not reasons
