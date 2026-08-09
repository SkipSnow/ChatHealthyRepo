# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for steps.discrepancy_report.emit_
discrepancy_report.

Covers:
  * emit reads pipeline.discrepancies from the pipeline cluster
    (Pipelines metadata database).
  * When manifest_doc has a truthy abort_reason, the email body starts
    with `FATAL: <abort_reason>`.
  * When manifest_doc has no abort_reason (or falsy), the FATAL line is
    absent.
"""

from __future__ import annotations

import pytest

from steps.discrepancy_report import emit_discrepancy_report


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)


class _RecordingColl:
    def __init__(self, docs):
        self._docs = list(docs)
        self.find_calls: list[dict] = []

    def find(self, filt):
        self.find_calls.append(dict(filt))
        run_id = filt.get("run_id")
        if run_id is None:
            return _FakeCursor(self._docs)
        return _FakeCursor([d for d in self._docs if d.get("run_id") == run_id])


class _Db:
    def __init__(self, colls):
        self._colls = colls

    def __getitem__(self, name):
        return self._colls[name]


class _Mongo:
    def __init__(self, dbs):
        self._dbs = dbs

    def __getitem__(self, name):
        return self._dbs[name]


class _CapturingNotifier:
    def __init__(self):
        self.emails: list[tuple] = []
        self.smses: list[tuple] = []

    def send_email(self, addr, subject, body, attachments=None):
        self.emails.append((addr, subject, body, attachments))

    def send_sms(self, addr, body):
        self.smses.append((addr, body))


def _mongo_with_discrepancies(rows):
    coll = _RecordingColl(rows)
    return _Mongo({"pipelineAdmin": _Db({"pipeline.discrepancies": coll})}), coll


def _stub_pdf(monkeypatch):
    # discrepancy_report imports build_discrepancy_pdf inside the function,
    # deliberately, so a missing reportlab does not break module import.
    # Patch it on the source module; the late import picks it up.
    import discrepancy_pdf
    monkeypatch.setattr(discrepancy_pdf, "build_discrepancy_pdf", lambda _md, _dx: b"PDFBYTES")


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_reads_pipeline_discrepancies_from_pipeline_cluster(monkeypatch):
    _stub_pdf(monkeypatch)
    rows = [
        {"run_id": "R1", "reason": "x"},
        {"run_id": "R1", "reason": "y"},
        {"run_id": "R2", "reason": "other-run"},
    ]
    mongo, coll = _mongo_with_discrepancies(rows)
    notifier = _CapturingNotifier()
    summary = emit_discrepancy_report(
        pipeline_mongo=mongo,
        run_id="R1",
        manifest_status="succeeded",
        manifest_doc={"run_id": "R1"},
        config={"notification_receivers": ["ops@example.com"]},
        notification_client=notifier,
    )
    assert summary["total"] == 2
    assert coll.find_calls == [{"run_id": "R1"}]


def test_abort_reason_prepends_fatal_line(monkeypatch):
    _stub_pdf(monkeypatch)
    mongo, _ = _mongo_with_discrepancies([])
    notifier = _CapturingNotifier()
    emit_discrepancy_report(
        pipeline_mongo=mongo,
        run_id="R1",
        manifest_status="failed",
        manifest_doc={"run_id": "R1", "abort_reason": "registry_dependency_cycle"},
        config={"notification_receivers": ["ops@example.com"]},
        notification_client=notifier,
    )
    assert len(notifier.emails) >= 1
    _addr, _subj, body, _atts = notifier.emails[0]
    lines = body.splitlines()
    assert lines[0] == "FATAL: registry_dependency_cycle"
    assert "run_id=R1" in body
    assert "status=failed" in body


def test_no_abort_reason_no_fatal_line(monkeypatch):
    _stub_pdf(monkeypatch)
    mongo, _ = _mongo_with_discrepancies([])
    notifier = _CapturingNotifier()
    emit_discrepancy_report(
        pipeline_mongo=mongo,
        run_id="R1",
        manifest_status="succeeded",
        manifest_doc={"run_id": "R1"},
        config={"notification_receivers": ["ops@example.com"]},
        notification_client=notifier,
    )
    _addr, _subj, body, _atts = notifier.emails[0]
    assert not body.startswith("FATAL:")
    assert "FATAL:" not in body


def test_falsy_abort_reason_no_fatal_line(monkeypatch):
    _stub_pdf(monkeypatch)
    mongo, _ = _mongo_with_discrepancies([])
    notifier = _CapturingNotifier()
    emit_discrepancy_report(
        pipeline_mongo=mongo,
        run_id="R1",
        manifest_status="succeeded",
        manifest_doc={"run_id": "R1", "abort_reason": ""},
        config={"notification_receivers": ["ops@example.com"]},
        notification_client=notifier,
    )
    _addr, _subj, body, _atts = notifier.emails[0]
    assert "FATAL:" not in body
