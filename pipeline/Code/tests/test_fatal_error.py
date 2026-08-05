# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Test: DiscrepancyReport emits fatal discrepancy."""

from __future__ import annotations

import os
from steps.discrepancy_report import DiscrepancyReport, fatal_error


def test_fatal_error():
    """DiscrepancyReport constructs and calls fatal_error via mTLS."""
    os.environ["ENV_PREFIX"] = "dev"
    os.environ["PIPELINE_NAME"] = "provider"

    report = DiscrepancyReport(
        run_id="test_fatal",
        env="dev",
        pipeline_name="provider",
        source="mongo_utilities.py:42",
    )

    result = fatal_error(
        report,
        level="fatal",
        explanation="vault name is not equal to the cert name",
        source_line="mongo_utilities.py:42",
    )

    assert result is True
