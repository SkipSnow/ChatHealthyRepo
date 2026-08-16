# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

PROVIDER_LIST_PROJECTION = {
    "_id": 0,
    "npi": 1,
    "entity_type_code": 1,
    "business_address": 1,
    "practice_addresses": 1,
    "taxonomies": 1,
    "licenses": 1,
}
