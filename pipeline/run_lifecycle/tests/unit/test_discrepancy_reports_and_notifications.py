# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for discrepancy_report.emit_discrepancy_report.

Covers, against the unified discrepancyLog store (LLD v54 §7.5):
  * emit counts only the findings belonging to the run it was asked about,
    summing the per-class type_aggregate counts.
  * the delivered report body names each finding class present.
  * the report is delivered on success and on abnormal end alike.

These read a real collection through the canonical utility. The destination
database and collection are redirected to a scratch collection dropped
afterwards, so a run never touches live pipeline metadata. The email
transport is the one thing substituted: sending is a paid third-party call
and what is under test is the body, not SparkPost.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def report_env(monkeypatch, scratch_mongo):
    """Real cluster, scratch discrepancyLog collection, captured email.

    Yields (client, discrepancy_log_collection, sent) where `sent` is the
    list of (addr, subject, body, attachments) the code tried to send.
    """
    import chathealthy_lib.discrepancy_pdf as discrepancy_pdf
    import chathealthy_lib.notification_client as notification_client
    import chathealthy_lib.discrepancy_report as dr

    db, collection = scratch_mongo
    discrepancy_log = collection("discrepancyLog")

    monkeypatch.setattr(dr, "PIPELINE_ADMIN_DB", db.name)
    monkeypatch.setattr(dr, "DISCREPANCY_LOG_COLLECTION", discrepancy_log.name)
    # Point the config load at an empty scratch collection so config_loaded is
    # deterministically False -- these emit tests exercise the REQ-B-006 escape
    # (store unreachable -> env recipient). The normal KV-secret recipient path
    # is covered by the recipient-resolution tests below.
    monkeypatch.setattr(dr, "PIPELINE_CONFIG_COLLECTION", collection("PipelineConfig").name)
    monkeypatch.setenv("NOTIFICATION_TO_EMAIL", "ops@example.com")

    # The PDF builder needs reportlab; the bytes are not what is asserted.
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


def _aggregate(run_id: str, finding_class: str, severity: str = "warning",
               count: int = 1, keys: list | None = None) -> dict:
    keys = keys if keys is not None else []
    return {
        "_id": f"{run_id}:provider:type:{finding_class}",
        "kind": "type_aggregate",
        "run_id": run_id,
        "artifact": "provider",
        "class": finding_class,
        "severity": severity,
        "count": count,
        "keys": keys,
    }


@pytest.mark.unit
def test_counts_only_the_requested_runs_discrepancies(report_env):
    from chathealthy_lib.discrepancy_report import emit_discrepancy_report

    client, discrepancy_log, _sent = report_env
    discrepancy_log.insert_many([
        _aggregate("R1", "county_unresolvable", count=1, keys=["1"]),
        _aggregate("R1", "state_missing_no_license", count=1, keys=["2"]),
        _aggregate("R2", "county_unresolvable", count=5, keys=["9"]),
    ])

    summary = emit_discrepancy_report(
        pipeline_mongo=client,
        run_id="R1",
        manifest_status="succeeded",
        manifest_doc={"run_id": "R1"},
        config={},
    )
    assert summary["total"] == 2, "the other run's findings must not be counted"


@pytest.mark.unit
def test_business_finding_renders_but_fatal_renders_operationally(report_env):
    from chathealthy_lib.discrepancy_report import emit_discrepancy_report

    client, discrepancy_log, sent = report_env
    discrepancy_log.insert_many([
        # A non-fatal business per-record finding: this renders in the findings
        # summary and must name its class in the body.
        _aggregate("R1", "county_unresolvable", severity="warning", count=1, keys=["1"]),
        # A fatal: it is NOT a per-record finding row. It renders only in the
        # operational abnormal-end header (Run status FAILED), never among the
        # findings.
        _aggregate("R1", "registry_dependency_cycle", severity="fatal", count=1),
    ])

    emit_discrepancy_report(
        pipeline_mongo=client,
        run_id="R1",
        manifest_status="failed",
        manifest_doc={"run_id": "R1"},
        config={},
    )
    assert sent, "a run with a fatal must deliver the report"
    for _addr, _subject, body, _attachments in sent:
        assert "county_unresolvable" in body, (
            "the report body must name each business per-record finding class"
        )
        assert "registry_dependency_cycle" not in body, (
            "a fatal must not appear as a finding-class row in the body"
        )
        assert "FAILED" in body, (
            "the abend renders in the operational abnormal-end header"
        )


@pytest.mark.unit
def test_report_delivered_on_success_with_warnings(report_env):
    from chathealthy_lib.discrepancy_report import emit_discrepancy_report

    client, discrepancy_log, sent = report_env
    discrepancy_log.insert_many([
        _aggregate("R1", "county_unresolvable", count=1, keys=["1"]),
        _aggregate("R1", "state_missing_no_license", severity="warning", count=1, keys=["2"]),
    ])

    emit_discrepancy_report(
        pipeline_mongo=client,
        run_id="R1",
        manifest_status="succeeded",
        manifest_doc={"run_id": "R1"},
        config={},
    )
    assert sent, "the report is delivered on success and abnormal end alike"


@pytest.mark.unit
def test_report_delivered_on_success_with_no_discrepancies(report_env):
    from chathealthy_lib.discrepancy_report import emit_discrepancy_report

    client, _discrepancy_log, sent = report_env
    summary = emit_discrepancy_report(
        pipeline_mongo=client,
        run_id="R1",
        manifest_status="succeeded",
        manifest_doc={"run_id": "R1"},
        config={},
    )
    assert summary["total"] == 0
    assert sent, "a clean run still delivers exactly one report"


# ---------- recipient resolution (FIX 1: KV-named subscriber secret) ----------


def _report():
    """A DiscrepancyReport whose recipient inputs the test sets directly.

    Construction fails open on the cluster/vault it cannot reach, which is
    exactly the state these tests then override attribute by attribute.
    """
    from chathealthy_lib.discrepancy_report import DiscrepancyReport
    return DiscrepancyReport(
        run_id="RREC",
        env="test",
        pipeline_name="provider",
        source="test",
        total_source_rows=None,
        rows_in_target=None,
        total_rows=None,
        target_collection="db.coll",
    )


@pytest.mark.unit
def test_recipients_from_kv_secret_when_config_loaded():
    report = _report()
    report.config_loaded = True
    report.config = {"metadata": {"subscribers_secret": "provider-report-subs"}}
    fetched = {}

    def _fake_secret(name):
        fetched["name"] = name
        return "a@example.com; b@example.ai, a@example.com"

    report._get_secret_value = _fake_secret
    receivers = report._resolve_recipients()
    assert fetched["name"] == "provider-report-subs", "reads the NAMED secret"
    assert receivers == ["a@example.com", "b@example.ai"], "parsed + de-duplicated"


@pytest.mark.unit
def test_raises_when_subscribers_secret_absent_and_config_loaded(monkeypatch):
    from chathealthy_lib.exceptions import ChatHealthyException
    monkeypatch.setenv("NOTIFICATION_TO_EMAIL", "ops@example.com")
    report = _report()
    report.config_loaded = True
    report.config = {"metadata": {}}
    with pytest.raises(ChatHealthyException) as ei:
        report._resolve_recipients()
    assert ei.value.mode == "discrepancy_report_subscribers_secret_missing"


@pytest.mark.unit
def test_raises_when_secret_yields_no_recipients_and_config_loaded():
    from chathealthy_lib.exceptions import ChatHealthyException
    report = _report()
    report.config_loaded = True
    report.config = {"metadata": {"subscribers_secret": "provider-report-subs"}}
    report._get_secret_value = lambda name: "   "  # empty after parse
    with pytest.raises(ChatHealthyException) as ei:
        report._resolve_recipients()
    assert ei.value.mode == "discrepancy_report_no_recipients"


@pytest.mark.unit
def test_env_fallback_only_when_config_store_unreachable(monkeypatch):
    monkeypatch.setenv("NOTIFICATION_TO_EMAIL", "ops@example.com")
    report = _report()
    report.config_loaded = False  # store unreachable -> REQ-B-006 escape
    receivers = report._resolve_recipients()
    assert receivers == ["ops@example.com"]


# ---------- INFO severity tier (a bucket below warning) ----------


def _agg(cls: str, severity: str, count: int, keys: list | None = None) -> dict:
    return {"class": cls, "severity": severity, "count": count,
            "keys": keys if keys is not None else []}


@pytest.mark.unit
def test_info_is_tallied_apart_from_warnings_and_errors():
    report = _report()
    model = report._report_model([
        _agg("county_unresolvable", "warning", 4),
        _agg("row_unparseable", "error", 2),
        _agg("phone_shape_uncertain", "info", 9),
    ], cap=25)
    assert model["warning_total"] == 4
    assert model["error_total"] == 2
    assert model["info_total"] == 9, "INFO has its own total"


@pytest.mark.unit
def test_info_does_not_make_a_run_a_failure():
    report = _report()
    model = report._report_model([_agg("phone_shape_uncertain", "info", 9)], cap=25)
    # A run with only INFO findings has no warnings, errors, or fatal.
    assert model["warning_total"] == 0
    assert model["error_total"] == 0
    assert model["fatal_total"] == 0
    assert model["info_total"] == 9


@pytest.mark.unit
def test_info_class_still_appears_in_the_per_class_body():
    report = _report()
    model = report._report_model([_agg("phone_shape_uncertain", "info", 9)], cap=25)
    classes = {(r["class"], r["severity"]) for r in model["summary"]}
    assert ("phone_shape_uncertain", "info") in classes


# ---------- run start/end resolve from the run manifest, not object build ----------


@pytest.mark.unit
def test_run_times_do_not_fabricate_a_distinct_end_without_a_manifest():
    report = _report()
    report.mongo_down = True  # no manifest reachable
    started, ended = report._run_times()
    assert started == report.start_time
    assert ended == started, "a report with no manifest must not invent a duration"


@pytest.mark.unit
def test_run_times_read_started_and_ended_from_the_manifest(monkeypatch):
    import uuid
    from datetime import datetime, timezone
    import chathealthy_lib.discrepancy_report as dr

    report = _report()
    if report.mongo_connection is None or report.mongo_down:
        pytest.skip("front-end metadata cluster unavailable")

    scratch = f"chathealthy_test_scratch_{uuid.uuid4().hex}_pipeline_runs"
    monkeypatch.setattr(dr, "PIPELINE_RUNS_COLLECTION", scratch)
    coll = report.mongo_connection[dr.PIPELINE_ADMIN_DB][scratch]
    started_dt = datetime(2026, 1, 15, 18, 30, tzinfo=timezone.utc)
    ended_dt = datetime(2026, 1, 15, 20, 45, tzinfo=timezone.utc)
    try:
        coll.insert_one({"run_id": report.run_id,
                         "started_at": started_dt, "ended_at": ended_dt})
        stored = coll.find_one({"run_id": report.run_id})
        started, ended = report._run_times()
        # Compare against _iso of the stored values so the assertion is robust
        # to whether the driver returns tz-aware or tz-naive datetimes.
        assert started == report._iso(stored["started_at"])
        assert ended == report._iso(stored["ended_at"])
        assert started != ended, "start and end are distinct instants"
    finally:
        try:
            report.mongo_connection[dr.PIPELINE_ADMIN_DB].drop_collection(scratch)
        except Exception:
            pass
