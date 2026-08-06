# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Integration test: DiscrepancyReport sends email with warnings/errors."""

import os
import sys
import uuid

sys.path.insert(0, "FrontEndApplicationLib/src")

from steps.discrepancy_report import DiscrepancyReport, fatal_error


def test_email_send_with_warnings_and_errors():
    """Test: create warnings/errors, then send email report."""

    # Setup
    os.environ["ENV_PREFIX"] = "dev"
    os.environ["PIPELINE_NAME"] = "provider"
    run_id = f"test_email_{uuid.uuid4().hex[:8]}"

    # Create report instance - uses REAL MongoDB connection to PIPELINE cluster
    report = DiscrepancyReport(
        run_id=run_id,
        env="dev",
        pipeline_name="provider",
        source="ProviderPipelineOnDemand",
    )

    # Create some warnings
    result1 = report.createWarning(
        job_name="provider",
        source="ProviderPipelineOnDemand",
        details="Test warning 1",
        data_source="NPPES",
        record_id="1003199654",
    )
    assert result1 is True

    # Create some errors
    result2 = report.createNonFatalError(
        job_name="provider",
        source="ProviderPipelineOnDemand",
        details="Test error 1",
        data_source="NUCC",
        record_id="207Q00000X",
    )
    assert result2 is True

    result3 = report.createNonFatalError(
        job_name="provider",
        source="ProviderPipelineOnDemand",
        details="Test error 2",
        data_source="NPPES",
        record_id="1003199655",
    )
    assert result3 is True

    # Simulate fatal error: certificate CN mismatch when webhook tries to connect to Mongo
    # fatal_error() calls _write_email() internally
    # Uses REAL MongoDB to store and retrieve all documents
    try:
        fatal_error(
            report=report,
            level="fatal",
            explanation="Certificate CN 'chpipeline-service' does not match expected 'pipelineEditor'",
        )

    except Exception as e:
        raise AssertionError(f"Test failed with exception: {type(e).__name__}: {e}") from e
