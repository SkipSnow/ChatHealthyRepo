# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from chathealthy_lib.discrepancy_pdf import (
    build_discrepancy_pdf,
    render_header_as_html,
    render_header_as_text,
)


def test_pdf_bytes():
    b = build_discrepancy_pdf({'run_id': 'r1', 'status': 'success', 'env': 'dev'}, [])
    assert isinstance(b, bytes) and len(b) > 0


def _manifest() -> dict:
    return {
        "run_id": "r1",
        "pipeline_name": "provider",
        "run_status": "succeeded",
        # Two distinct instants: 18:30 UTC -> 10:30 AM PST, 20:45 UTC -> 12:45 PM PST.
        "run_started_utc": "2026-01-15T18:30:00+00:00",
        "run_ended_utc": "2026-01-15T20:45:00+00:00",
        "fatal_reason": "",
        "records_successfully_collected": 900,
        "records_with_non_certain_information": 12,
        "records_with_non_fatal_warnings": 7,
        "records_with_non_fatal_errors": 3,
        "rows_in_target": 903,
        "total_rows": 5000,
        "target_collection": "Providers",
    }


def test_header_shows_distinct_start_and_end_times():
    html = render_header_as_html(_manifest(), [])
    assert "1/15/2026 10:30 AM PST" in html, "run start must come from run_started_utc"
    assert "1/15/2026 12:45 PM PST" in html, "run end must come from run_ended_utc"


def test_header_labels_match_the_revised_report():
    text = render_header_as_text(_manifest(), [])
    assert "Records successfully collected" in text
    assert "Records with non certain Information" in text
    assert "Records with non-fatal warnings" in text
    assert "Records with non-fatal errors" in text
    # The retired "100%" framing must be gone.
    assert "100%" not in text
    assert "successfully collected" in text.lower()


def test_non_certain_information_count_reaches_the_header():
    html = render_header_as_html(_manifest(), [])
    assert "Records with non certain Information" in html
    assert ">12<" in html, "the INFO count is displayed against its own row"


def test_info_summary_row_renders_in_pdf():
    summary = [{"class": "phone_shape_uncertain", "severity": "info", "count": 12}]
    b = build_discrepancy_pdf(_manifest(), summary, appendix=summary)
    assert isinstance(b, bytes) and len(b) > 0
