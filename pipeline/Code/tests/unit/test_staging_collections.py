# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

from __future__ import annotations

import pytest

from staging_collections import load_staging_collection


class _FakeColl:
    def __init__(self):
        self.docs = []
        self.dropped = False

    def drop(self):
        self.dropped = True
        self.docs = []

    def insert_many(self, docs, ordered=False):
        self.docs.extend(docs)


class _FakeDB:
    def __init__(self):
        self.colls = {}

    def __getitem__(self, name):
        return self.colls.setdefault(name, _FakeColl())


@pytest.mark.unit
def test_full_load_drop_and_insert():
    db = _FakeDB()
    out = load_staging_collection(db, "pipeline_sources_nppes_npi", [{"npi": "1"}], incremental=False)
    assert out["mode"] == "full"
    assert out["inserted"] == 1
    assert db["pipeline_sources_nppes_npi"].dropped is True
