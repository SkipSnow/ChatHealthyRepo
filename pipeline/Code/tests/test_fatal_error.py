# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Test: fatal_error handles fatal discrepancies."""

from __future__ import annotations

from unittest.mock import Mock

from steps import discrepancy_report


def test_fatal_error():
    """fatal_error() constructs and sends fatal error report."""
    mock_mongo_conn = {
        "chathealthypipelines": {
            "pipeline.discrepancies": Mock(),
            "pipeline.discrepancy_reports": Mock(),
        }
    }

    mock_report = Mock()
    mock_report.mongo_connection = mock_mongo_conn
    mock_report.run_id = "test_fatal"
    mock_report.env = "dev"
    mock_report.pipeline_name = "provider"
    mock_report.source = "test_source.py:42"
    mock_report.start_time = "2026-08-05T12:00:00Z"
    mock_report.config = {
        "warning_threshold": 10,
        "error_threshold": 5,
        "warnings_errors_collection": "pipeline.discrepancies",
    }
    mock_report.write_email = Mock(return_value=True)
    mock_report.get_counts = Mock(return_value={"warnings": 0, "errors": 0, "fatals": 1})

    result = discrepancy_report.fatal_error(
        mock_report,
        level="fatal",
        explanation="vault name is not equal to the cert name",
        source_line="mongo_utilities.py:42",
    )

    assert result is True
    mock_report.write_email.assert_called_once()
