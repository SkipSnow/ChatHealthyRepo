# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Integration test: a fatal delivers the one report from discrepancyLog.

Findings live in the unified pipelineAdmin.discrepancyLog store as per-class
type_aggregate documents (LLD v54 §7.5). A fatal records its job-level
aggregate and delivers the report. The store is a real scratch collection
reached through the canonical utility; only the paid email transport is
substituted.
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


def test_fatal_records_aggregate_and_delivers_report(fatal_env):
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

    # The job-level fatal was recorded to discrepancyLog as a type_aggregate.
    fatal_aggs = list(discrepancy_log.find(
        {"run_id": run_id, "kind": "type_aggregate", "severity": "fatal"}))
    assert len(fatal_aggs) == 1, "exactly one fatal aggregate per run"

    for _addr, _subject, body, _attachments in sent:
        assert "Certificate CN" in body or fatal_aggs[0]["class"] in body
