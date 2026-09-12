# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests — within-record license and insurance dedup."""

from __future__ import annotations

import pytest

from record_subdoc_dedup import dedupe_insurance, dedupe_licenses, merge_licenses, merge_insurance


@pytest.mark.unit
def test_dedupe_licenses_same_state_and_id():
    licenses = [
        {"state": "VT", "license_id": "097.0135742", "status": "active"},
        {"state": "vt", "number": "097.0135742", "status": "active", "state_label": "Vermont"},
    ]
    out = dedupe_licenses(licenses)
    assert len(out) == 1
    assert out[0]["license_id"] == "097.0135742" or out[0].get("number") == "097.0135742"


@pytest.mark.unit
def test_dedupe_licenses_different_ids_same_state_kept():
    licenses = [
        {"state": "VT", "license_id": "089.0137055"},
        {"state": "VT", "license_id": "097.0135742"},
    ]
    assert len(dedupe_licenses(licenses)) == 2


@pytest.mark.unit
def test_dedupe_insurance_same_type_and_state():
    insurance = [
        {"insurance_type": "05", "insurance_type_label": "MEDICAID", "state": "WY", "state_label": "Wyoming"},
        {"insurance_type": "05", "insurance_type_label": "Medicaid", "state": "WY", "state_label": "WY"},
    ]
    assert len(dedupe_insurance(insurance)) == 1


@pytest.mark.unit
def test_merge_insurance_appends_new_slot():
    existing = [{"insurance_type": "05", "state": "WY", "insurance_type_label": "MEDICAID", "state_label": "WY"}]
    incoming = [{"insurance_type": "01", "state": "VT", "insurance_type_label": "OTHER", "state_label": "VT"}]
    merged = merge_insurance(existing, incoming)
    assert len(merged) == 2


@pytest.mark.unit
def test_merge_licenses_dedupes_on_append():
    existing = [{"state": "VT", "license_id": "A1"}]
    incoming = [{"state": "VT", "number": "A1", "state_label": "Vermont"}]
    assert len(merge_licenses(existing, incoming)) == 1
