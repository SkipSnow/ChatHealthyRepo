# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""LLD §9.2 — failure threshold abort and incremental staging."""

import pytest

from post_load_reconciliation import reconcile_npi_sets
from tests.regression.assert_business_rules import assert_failure_threshold_aborts


@pytest.mark.unit
def test_failure_threshold_aborts_manifest():
    manifest = {"status": "failed", "abort_reason": "discrepancy_threshold_exceeded count=1001 threshold=1000"}
    ok, detail = assert_failure_threshold_aborts(manifest, threshold=1000, discrepancy_count=1001)
    assert ok, detail


@pytest.mark.unit
def test_failure_threshold_allows_under_limit():
    manifest = {"status": "completed"}
    ok, detail = assert_failure_threshold_aborts(manifest, threshold=1000, discrepancy_count=50)
    assert ok, detail


@pytest.mark.unit
def test_reconcile_incremental_extra_npis():
    summary = reconcile_npi_sets({"1"}, {"1", "2"})
    assert summary["extra"] == ["2"]
    assert summary["missing"] == []
