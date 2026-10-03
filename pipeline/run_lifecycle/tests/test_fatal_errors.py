# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Integration test: an infra fatal delivers the one report operationally.

Business per-record findings live in the unified pipelineAdmin.discrepancyLog
store as per-class type_aggregate documents (LLD v54 §7.5). An infra/operational
fatal is NOT a per-record finding: it delivers the report and renders in the
operational abnormal-end header (Run status FAILED + fatal reason), but it is
never written to discrepancyLog. The store is a real scratch collection reached
through the canonical utility; only the paid email transport is substituted.
"""

import uuid

import pytest

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / '.git').exists():
        _lib = _d / 'ChatHealthyLib' / 'src'
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break


@pytest.fixture
def fatal_env(monkeypatch, scratch_mongo):
    import chathealthy_lib.discrepancy_pdf as discrepancy_pdf
    import chathealthy_lib.notification_client as notification_client
    import chathealthy_lib.discrepancy_report as dr

    db, collection = scratch_mongo
    discrepancy_log = collection("discrepancyLog")

    monkeypatch.setattr(dr, "PIPELINE_ADMIN_DB", db.name)
    monkeypatch.setattr(dr, "DISCREPANCY_LOG_COLLECTION", discrepancy_log.name)
    # Config store deterministically unreachable -> REQ-B-006 env escape.
    monkeypatch.setattr(dr, "PIPELINE_CONFIG_COLLECTION", collection("PipelineConfig").name)
    monkeypatch.setenv("NOTIFICATION_TO_EMAIL", "ops@example.com")
    monkeypatch.setattr(discrepancy_pdf, "build_discrepancy_pdf",
                        lambda _m, _summary, _appendix=None: b"PDFBYTES")

    sent: list[tuple] = []

    class _CapturingClient:
        def send_email(self, addr, subject, body, attachments=None, **kw):
            sent.append((addr, subject, body, attachments))
            return True

        def send_sms(self, addr, body):
            return True

    monkeypatch.setattr(notification_client, "NotificationClient", _CapturingClient)
    return db.client, discrepancy_log, sent


def test_infra_fatal_delivers_report_without_writing_a_finding(fatal_env):
    from chathealthy_lib.discrepancy_report import DiscrepancyReport, fatal_error

    client, discrepancy_log, sent = fatal_env
    run_id = f"test_fatal_{uuid.uuid4().hex[:8]}"

    report = DiscrepancyReport(
        run_id=run_id,
        env="local",
        pipeline_name="provider",
        source="ProviderPipelineOnDemand",
        target_collection="PipelinePublicHealthData.Provider_v_3",
        total_source_rows=8214553,
        rows_in_target=0,
        total_rows=8214550,
        data_version=3,
    )
    # Redirect the report's reads/writes to the scratch cluster.
    report.mongo_connection = client
    report.mongo_down = False

    delivered = fatal_error(
        report,
        level="fatal",
        explanation="Certificate CN 'chpipeline-service' does not match "
                    "expected 'pipelineEditor'",
    )

    assert delivered is True, "a fatal must deliver the one report"
    assert sent, "the fatal report must reach at least one recipient"

    # fatal_error raises mode job_fatal -- an infra/operational fatal, NOT a
    # per-record business finding. It MUST NOT be written to discrepancyLog.
    fatal_aggs = list(discrepancy_log.find(
        {"run_id": run_id, "kind": "type_aggregate", "severity": "fatal"}))
    assert fatal_aggs == [], "an infra fatal must not land a finding in discrepancyLog"

    # It renders only through the operational abnormal-end header: Run status
    # FAILED and the fatal reason naming the failure.
    for _addr, _subject, body, _attachments in sent:
        assert "Certificate CN" in body, "the fatal reason names the failure"
        assert "FAILED" in body, "the run renders as a failed abnormal end"
