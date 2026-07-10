# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from provider_flags_enrichment import compute_provider_flags
def test_disqualified_empty_taxonomies():
    flags = compute_provider_flags({'entity_type_code': '1', 'taxonomies': []}, {})
    assert flags['is_disqualified'] is True
