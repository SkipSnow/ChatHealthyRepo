# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for pipeline_fatal_recorder.record_fatal_discrepancy.

The store is a real scratch collection reached through the canonical utility;
the front-end handle is redirected to the scratch cluster via the module's
seam. No fake, mock or stub stands in for MongoDB.
"""

from __future__ import annotations

import pytest

from chathealthy_lib.exceptions import ChatHealthyException

import pipeline.run_lifecycle.pipeline_fatal_recorder as recorder
from pipeline.run_lifecycle.pipeline_fatal_recorder import record_fatal_discrepancy


def _make_infra_exception():
    """An infra/operational fatal -- it must NEVER reach discrepancyLog."""
    return ChatHealthyException(
        mode="registry_test_mode",
        message="unit-test failure",
        offending_field="x",
    )


def _make_business_exception(already_recorded: bool = False):
    """A true per-record business finding escalation (the one allowed mode).

    write_finding always stamps already_recorded_fatal on the real event; the
    unit tests exercise the gate's allow branch with the flag absent, and the
    dedup guard with it present.
    """
    context = {"offending_field": "x"}
    if already_recorded:
        context["already_recorded_fatal"] = True
    return ChatHealthyException(
        mode="pipeline_fatal_finding",
        message="unit-test failure",
        **context,
    )


@pytest.fixture
def scratch_recorder(monkeypatch, scratch_mongo):
    db, collection = scratch_mongo
    disc = collection("discrepancyLog")
    monkeypatch.setattr(recorder, "_frontend_mongo", lambda: db.client)
    monkeypatch.setattr(recorder, "FATAL_DISCREPANCIES_DB", db.name)
    monkeypatch.setattr(recorder, "FATAL_DISCREPANCIES_COLL", disc.name)
    return disc


def test_business_finding_mode_records_expected_shape(scratch_recorder):
    disc = scratch_recorder
    exc = _make_business_exception()
    record_fatal_discrepancy(None, run_id="run-1", step="test_step", exc=exc)

    docs = list(disc.find({"kind": "type_aggregate"}))
    assert len(docs) == 1
    doc = docs[0]
    assert doc["run_id"] == "run-1"
    assert doc["class"] == "fatal_pipeline_fatal_finding"
    assert doc["severity"] == "fatal"
    assert doc["artifact"] == "run"
    assert doc["count"] == 1
    assert doc["step"] == "test_step"
    assert doc["context"]["mode"] == "pipeline_fatal_finding"
    assert doc["context"]["message"] == "unit-test failure"
    assert doc["context"]["fields"] == {"offending_field": "x"}


def test_infra_mode_writes_no_finding(scratch_recorder):
    # An infra/operational mode must NEVER land a finding in discrepancyLog;
    # it is reported through the operational channel instead.
    disc = scratch_recorder
    record_fatal_discrepancy(
        None, run_id="run-infra", step="test_step", exc=_make_infra_exception())
    assert list(disc.find({})) == []


def test_already_recorded_business_mode_writes_no_second_marker(scratch_recorder):
    # write_finding already recorded this event; the recorder must not add a
    # second (job-level) marker, so a run records exactly one fatal.
    disc = scratch_recorder
    record_fatal_discrepancy(
        None, run_id="run-dedup", step="test_step",
        exc=_make_business_exception(already_recorded=True))
    assert list(disc.find({})) == []


def test_swallows_mongo_write_failure(monkeypatch, scratch_recorder):
    # A write that raises must be swallowed; the recorder never raises. Exercise
    # the real write path with the one mode that reaches it (business finding).
    def _boom():
        raise ChatHealthyException(
            mode="db_unreachable",
            component="test_pipeline_fatal_recorder",
            message="pipeline cluster unreachable")

    monkeypatch.setattr(recorder, "_frontend_mongo", _boom)
    record_fatal_discrepancy(
        None, run_id="run-2", step="test_step", exc=_make_business_exception())


def test_primary_exception_unblocked_after_recorder_failure(monkeypatch, scratch_recorder):
    """If the recorder's write blows up, the caller's original exception must
    still be raised (proven here by simulating the caller pattern)."""
    def _boom():
        raise ChatHealthyException(
            mode="db_unreachable", component="test", message="down")

    monkeypatch.setattr(recorder, "_frontend_mongo", _boom)
    exc = _make_business_exception()
    with pytest.raises(ChatHealthyException) as ei:
        record_fatal_discrepancy(None, run_id="r", step="s", exc=exc)
        raise exc
    assert ei.value.mode == "pipeline_fatal_finding"
