# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from provider_record_builder import build_provider_record


def test_build_individual_record(nppes_individual_row):
    doc = build_provider_record(nppes_individual_row, npi="1234567890", run_id="run-1")
    assert doc["npi"] == "1234567890"
    assert doc["entity_type_code"] == "1"
    assert doc["run_id"] == "run-1"
    assert doc.get("taxonomies")


def test_build_org_record(nppes_org_row):
    doc = build_provider_record(nppes_org_row, npi="9876543210")
    assert doc["entity_type_code"] == "2"
    assert doc["entity_type_code_label"] == "institutional"
