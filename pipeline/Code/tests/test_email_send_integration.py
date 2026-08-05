# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Integration test: DiscrepancyReport sends email with warnings/errors."""

import os
import uuid
from steps.discrepancy_report import DiscrepancyReport, createWarning, CreateNonFatalError


def test_email_send_with_warnings_and_errors():
    """Test: create warnings/errors, then send email report."""

    # Setup
    os.environ["ENV_PREFIX"] = "dev"
    os.environ["PIPELINE_NAME"] = "provider"
    run_id = f"test_email_{uuid.uuid4().hex[:8]}"

    # Create report instance
    report = DiscrepancyReport(
        run_id=run_id,
        env="dev",
        pipeline_name="provider",
        source="test_integration",
    )

    # Create some warnings
    result1 = createWarning(
        report=report,
        job_name="provider",
        source="test_source_1",
        details="Test warning 1",
        data_source="NPPES",
        record_id="1003199654",
    )
    print(f"createWarning 1 returned: {result1}")
    assert result1 is True

    # Create some errors
    result2 = CreateNonFatalError(
        report=report,
        job_name="provider",
        source="test_source_2",
        details="Test error 1",
        data_source="NUCC",
        record_id="207Q00000X",
    )
    print(f"CreateNonFatalError 1 returned: {result2}")
    assert result2 is True

    # Get counts
    counts = report.get_counts()
    print(f"Counts after warnings/errors: {counts}")

    # Now send email with all discrepancies
    email_result = report.write_email()
    print(f"write_email() returned: {email_result}")

    # This should be True if email was sent
    assert email_result is True, "write_email() returned False - email not sent"
    print("✓ Email sent successfully")


if __name__ == "__main__":
    test_email_send_with_warnings_and_errors()
    print("\n✓ Test passed - check your email for the discrepancy report")
