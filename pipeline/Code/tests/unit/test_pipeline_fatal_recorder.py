# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for pipeline_fatal_recorder.record_fatal_discrepancy."""

from __future__ import annotations

import datetime

import pytest

from chathealthy_frontend_lib.exceptions import ChatHealthyException

from pymongo.errors import PyMongoError

from pipeline_fatal_recorder import (
    FATAL_DISCREPANCIES_COLL,
    FATAL_DISCREPANCIES_DB,
    record_fatal_discrepancy,
)


class _CapturingColl:
    def __init__(self):
        self.docs: list[dict] = []

    def insert_one(self, doc):
        self.docs.append(dict(doc))


class _RaisingColl:
    def insert_one(self, doc):
        raise PyMongoError("pipeline cluster unreachable")


class _Db:
    def __init__(self, colls):
        self._colls = colls

    def __getitem__(self, name):
        return self._colls[name]


class _Mongo:
    def __init__(self, db_name, coll_name, coll):
        self._dbs = {db_name: _Db({coll_name: coll})}

    def __getitem__(self, name):
        return self._dbs[name]


def _make_exception():
    return ChatHealthyException(
        mode="registry_test_mode",
        message="unit-test failure",
        offending_field="x",
    )


def test_records_expected_shape():
    coll = _CapturingColl()
    mongo = _Mongo(FATAL_DISCREPANCIES_DB, FATAL_DISCREPANCIES_COLL, coll)
    exc = _make_exception()
    record_fatal_discrepancy(mongo, run_id="run-1", step="test_step", exc=exc)
    assert len(coll.docs) == 1
    doc = coll.docs[0]
    assert doc["run_id"] == "run-1"
    assert doc["reason"] == "fatal_registry_test_mode"
    assert doc["step"] == "test_step"
    assert doc["entity_kind"] == "pipeline_fatal"
    assert doc["npi"] is None
    assert doc["context"]["mode"] == "registry_test_mode"
    assert doc["context"]["message"] == "unit-test failure"
    assert doc["context"]["fields"] == {"offending_field": "x"}
    assert isinstance(doc["recorded_at"], datetime.datetime)
    assert isinstance(doc["created_at"], datetime.datetime)


def test_swallows_mongo_write_failure():
    coll = _RaisingColl()
    mongo = _Mongo(FATAL_DISCREPANCIES_DB, FATAL_DISCREPANCIES_COLL, coll)
    exc = _make_exception()
    # Must not raise; secondary failure is logged and swallowed.
    record_fatal_discrepancy(mongo, run_id="run-2", step="test_step", exc=exc)


def test_none_mongo_is_noop():
    exc = _make_exception()
    record_fatal_discrepancy(None, run_id="run-3", step="test_step", exc=exc)


def test_primary_exception_unblocked_after_recorder_failure():
    """If the recorder's write blows up, the caller's original exception
    must still be raised (proven here by simulating the caller pattern)."""
    coll = _RaisingColl()
    mongo = _Mongo(FATAL_DISCREPANCIES_DB, FATAL_DISCREPANCIES_COLL, coll)
    exc = _make_exception()
    with pytest.raises(ChatHealthyException) as ei:
        record_fatal_discrepancy(mongo, run_id="r", step="s", exc=exc)
        raise exc
    assert ei.value.mode == "registry_test_mode"
