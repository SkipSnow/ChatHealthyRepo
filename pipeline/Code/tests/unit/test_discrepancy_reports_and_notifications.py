# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for steps.discrepancy_report.emit_discrepancy_report.

Covers:
  * emit counts only the discrepancies belonging to the run it was asked
    about, reading them from the pipeline cluster.
  * When the run recorded a fatal, the email carries that fatal's
    explanation.
  * With no fatal recorded, no email is sent at all.

These read a real collection. The discrepancy row layout, the run_id
filter and the fatal/error/warning split are all things the server
answers, and a stand-in answers them however it was written to. The
destination database and collection are redirected to a scratch
collection that is dropped afterwards, so a run never touches live
pipeline metadata.

The email transport is the one thing substituted: sending is a paid
third-party call and what is under test is the body, not SparkPost.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def report_env(monkeypatch, scratch_mongo):
    """Real cluster, scratch discrepancies collection, captured email.

    Yields (client, discrepancies_collection, sent) where `sent` is the
    list of (addr, subject, body, attachments) the code tried to send.
    """
    import chathealthy_lib.discrepancy_pdf as discrepancy_pdf
    import chathealthy_lib.notification_client as notification_client
    import chathealthy_lib.discrepancy_report as dr

    db, collection = scratch_mongo
    discrepancies = collection("discrepancies")

    monkeypatch.setattr(dr, "PIPELINE_ADMIN_DB", db.name)
    monkeypatch.setattr(dr, "DISCREPANCIES_COLLECTION", discrepancies.name)
    monkeypatch.setenv("NOTIFICATION_TO_EMAIL", "ops@example.com")

    # The PDF builder needs reportlab; the bytes are not what is asserted.
    monkeypatch.setattr(discrepancy_pdf, "build_discrepancy_pdf",
                        lambda _m, _d: b"PDFBYTES")

    sent: list[tuple] = []

    class _CapturingClient:
        def send_email(self, addr, subject, body, attachments=None, **kw):
            sent.append((addr, subject, body, attachments))
            return True

        def send_sms(self, addr, body):
            return True

    monkeypatch.setattr(notification_client, "NotificationClient", _CapturingClient)
    return db.client, discrepancies, sent


def _row(run_id: str, level: str = "warning", **extra) -> dict:
    row = {
        "run_id": run_id,
        "reason": extra.pop("reason", "x"),
        "step": "provider",
        "entity_kind": "provider",
        "level": level,
        "npi": None,
    }
    row.update(extra)
    return row


@pytest.mark.unit
def test_counts_only_the_requested_runs_discrepancies(report_env):
    from chathealthy_lib.discrepancy_report import emit_discrepancy_report

    client, discrepancies, _sent = report_env
    discrepancies.insert_many([
        _row("R1", reason="x"),
        _row("R1", reason="y"),
        _row("R2", reason="other-run"),
    ])

    summary = emit_discrepancy_report(
        pipeline_mongo=client,
        run_id="R1",
        manifest_status="succeeded",
        manifest_doc={"run_id": "R1"},
        config={},
    )
    assert summary["total"] == 2, "the other run's discrepancy must not be counted"


@pytest.mark.unit
def test_fatal_explanation_reaches_the_email(report_env):
    from chathealthy_lib.discrepancy_report import emit_discrepancy_report

    client, discrepancies, sent = report_env
    discrepancies.insert_one(
        _row("R1", level="fatal", explanation="registry_dependency_cycle")
    )

    emit_discrepancy_report(
        pipeline_mongo=client,
        run_id="R1",
        manifest_status="failed",
        manifest_doc={"run_id": "R1"},
        config={},
    )
    assert sent, "a run with a fatal must send an email"
    # Recipient count is a config question, not this test's business; every
    # message that goes out must carry the reason.
    for _addr, _subject, body, _attachments in sent:
        assert "registry_dependency_cycle" in body, (
            "the email must name why the run died; the fatal row's "
            "explanation is where that comes from"
        )


@pytest.mark.unit
def test_no_email_when_the_run_recorded_no_fatal(report_env):
    from chathealthy_lib.discrepancy_report import emit_discrepancy_report

    client, discrepancies, sent = report_env
    discrepancies.insert_many([_row("R1"), _row("R1", level="error")])

    emit_discrepancy_report(
        pipeline_mongo=client,
        run_id="R1",
        manifest_status="succeeded",
        manifest_doc={"run_id": "R1"},
        config={},
    )
    assert sent == [], "warnings and errors alone do not raise an email"


@pytest.mark.unit
def test_no_email_when_the_run_has_no_discrepancies_at_all(report_env):
    from chathealthy_lib.discrepancy_report import emit_discrepancy_report

    client, _discrepancies, sent = report_env
    summary = emit_discrepancy_report(
        pipeline_mongo=client,
        run_id="R1",
        manifest_status="succeeded",
        manifest_doc={"run_id": "R1"},
        config={},
    )
    assert summary["total"] == 0
    assert sent == []
