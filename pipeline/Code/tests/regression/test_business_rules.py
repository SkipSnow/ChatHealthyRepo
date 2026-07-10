# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from regression.assert_business_rules import assert_npi_and_entity_type
def test_npi_rule():
    ok, violations = assert_npi_and_entity_type(
        [{"npi": "1234567890", "entity_type_code": "1"}]
    )
    assert ok, violations
