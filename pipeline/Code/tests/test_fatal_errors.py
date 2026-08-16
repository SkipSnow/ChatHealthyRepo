# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Integration test: DiscrepancyReport sends email with warnings/errors."""

import os
import sys
import uuid

sys.path.insert(0, "ChatHealthyLib/src")

from chathealthy_lib.discrepancy_report import DiscrepancyReport, fatal_error
import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / '.git').exists():
        _lib = _d / 'ChatHealthyLib' / 'src'
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException


def test_email_send_with_warnings_and_errors():
    """Test: create warnings/errors, then send email report."""

    # Setup
    os.environ["ENV_PREFIX"] = "local"
    os.environ["PIPELINE_NAME"] = "provider"
    run_id = f"test_email_{uuid.uuid4().hex[:8]}"

    # Create report instance - uses REAL MongoDB connection to PIPELINE cluster
    # The three row counts are the caller's responsibility: they count data on
    # the pipeline cluster, which the report never touches. A real workbook
    # queries them; the test supplies representative values.
    report = DiscrepancyReport(
        run_id=run_id,
        env="local",
        pipeline_name="provider",
        source="ProviderPipelineOnDemand",
        target_collection="PipelinePublicHealthData.Provider_v_3",
        total_source_rows=8214553,
        rows_in_target=0,
        total_rows=8214550,
        fatal_error=True,
        data_version=3,
    )

    # Create some warnings
    result1 = report.createWarning(
        job_name="provider",
        source="ProviderPipelineOnDemand",
        details=(
            "Practice address ZIP 940271234 is 9 digits with no hyphen; "
            "normalized to 94027-1234. County lookup used the 5-digit prefix."
        ),
        data_source="NPPES",
        record_id="1003199654",
        source_line="npidata_pfile_20260701.csv:184223",
        npi="1003199654",
        field_list=["provider_business_practice_location_address_postal_code",
                    "practice_county"],
    )
    assert result1 is True

    # Create some errors
    result2 = report.createNonFatalError(
        job_name="provider",
        source="ProviderPipelineOnDemand",
        details=(
            "Taxonomy code 207Q00000X is flagged primary on two rows for the "
            "same NPI; neither row carries a license number, so the primary "
            "specialty cannot be resolved."
        ),
        data_source="NUCC",
        record_id="1003199801",
        source_line="npidata_pfile_20260701.csv:184224",
        npi="1003199801",
        field_list=["healthcare_provider_taxonomy_code_1",
                    "healthcare_provider_primary_taxonomy_switch_1",
                    "provider_license_number_1"],
    )
    assert result2 is True

    result3 = report.createNonFatalError(
        job_name="provider",
        source="ProviderPipelineOnDemand",
        details=(
            "Practice address state 'ZZ' is not a USPS state code, so the row "
            "cannot be assigned a county and is excluded from geographic search."
        ),
        data_source="NPPES",
        record_id="1003199655",
        source_line="npidata_pfile_20260701.csv:184225",
        npi="1003199655",
        field_list=["provider_business_practice_location_address_state_name",
                    "practice_county",
                    "practice_fips"],
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
        raise ChatHealthyException(
            mode="assertion_failed",
            component="test_fatal_errors",
            message=f"Test failed with exception: {type(e).__name__}: {e}") from e
