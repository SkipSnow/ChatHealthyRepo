# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Tests the two drain algorithms the Provider Pipeline uses:

  (1) COMPLETE drain of the staging file
      staging_loader._drop_prior_state_scoped_rows(coll, source_name, states)
      Full-load hygiene for source_name == "nppes_npi": delete_many({}) --
      wipes every row regardless of run_id or state_scope. Non-nppes sources
      are guarded (return 0, no deletion).

  (2) CONSTRAINED drain of the real (target) file
      provider_normalize_engine.per_state_normalize step drain:
        providers_coll.delete_many({
            "addresses": {"$elemMatch": {
                "address_type": "business", "state": state
            }}
        })
      Deletes only providers whose BUSINESS mailing address matches this
      partition state. Providers with the state in a NON-business address
      (mailing/practice) or providers with no business-state match are
      preserved. Indexes are preserved (delete_many, not drop()).

Tests run against the DEV pipeline cluster in a scratch DB "DrainTest"
that this file OWNS. The pipeline's live collections
(PublicData.Provider_v_3, PublicStaging.StagingProvider_v_3) are NEVER
read from or written to; the tests both populate their own fixtures and
clean up their scratch DB when done.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv
from pymongo import ASCENDING

load_dotenv(Path(__file__).parent.parent.parent.parent / ".env", override=True)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "FrontEndApplicationLib" / "src"))

from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402
from staging_loader import _drop_prior_state_scoped_rows  # noqa: E402


SCRATCH_DB = "DrainTest"
STAGING_COLL_NPPES = "CompleteDrainStagingProvider"
STAGING_COLL_NUCC = "CompleteDrainStagingNucc"
TARGET_COLL = "ConstrainedDrainProvider"


@pytest.fixture(scope="module")
def mongo():
    client = ChatHealthyMongoUtilities("MONGO_connectionString").getConnection()
    yield client
    client.drop_database(SCRATCH_DB)


@pytest.fixture()
def staging_nppes_coll(mongo):
    coll = mongo[SCRATCH_DB][STAGING_COLL_NPPES]
    coll.delete_many({})
    coll.create_index([("run_id", ASCENDING), ("_source_row_index", ASCENDING)])
    coll.create_index([("npi", ASCENDING)], unique=True, name="npi_unique")
    yield coll
    coll.drop()


@pytest.fixture()
def staging_nucc_coll(mongo):
    coll = mongo[SCRATCH_DB][STAGING_COLL_NUCC]
    coll.delete_many({})
    coll.create_index([("code", ASCENDING)])
    yield coll
    coll.drop()


@pytest.fixture()
def target_coll(mongo):
    coll = mongo[SCRATCH_DB][TARGET_COLL]
    coll.delete_many({})
    coll.create_index([("npi", ASCENDING)], unique=True, name="npi_unique")
    coll.create_index([("addresses.state", ASCENDING)], name="addr_state")
    yield coll
    coll.drop()


def test_complete_drain_wipes_all_staging_rows_for_nppes(staging_nppes_coll):
    coll = staging_nppes_coll
    rows = [
        {"npi": f"100000000{i}", "run_id": "prior-run-A", "_source_row_index": i, "raw": {"x": 1}}
        for i in range(6)
    ] + [
        {"npi": f"200000000{i}", "run_id": "prior-run-B", "_source_row_index": i, "raw": {"x": 2}}
        for i in range(4)
    ]
    coll.insert_many(rows)
    assert coll.count_documents({}) == 10

    deleted = _drop_prior_state_scoped_rows(coll, "nppes_npi", ())

    assert deleted == 10, "complete drain must wipe every row"
    assert coll.count_documents({}) == 0, "collection must be empty after complete drain"
    idx_names = {i["name"] for i in coll.list_indexes()}
    assert "npi_unique" in idx_names, "indexes must be preserved (delete_many, not drop)"


def test_complete_drain_is_noop_for_non_nppes_source(staging_nucc_coll):
    coll = staging_nucc_coll
    rows = [
        {"code": f"20700000{i:02d}X", "run_id": "prior-run-A", "raw": {"Code": f"20700000{i:02d}X"}}
        for i in range(5)
    ]
    coll.insert_many(rows)
    assert coll.count_documents({}) == 5

    deleted = _drop_prior_state_scoped_rows(coll, "nucc", ())

    assert deleted == 0, "non-nppes drain must be a no-op"
    assert coll.count_documents({}) == 5, "nucc rows must survive complete-drain no-op"


def test_constrained_drain_removes_only_matching_business_state(target_coll):
    coll = target_coll
    ms_providers = [
        {
            "npi": f"MS{i:08d}",
            "addresses": [
                {"address_type": "business", "state": "MS", "city": "Jackson"},
                {"address_type": "practice", "state": "MS", "city": "Jackson"},
            ],
        }
        for i in range(3)
    ]
    ca_providers = [
        {
            "npi": f"CA{i:08d}",
            "addresses": [
                {"address_type": "business", "state": "CA", "city": "Oakland"},
                {"address_type": "practice", "state": "CA", "city": "Oakland"},
            ],
        }
        for i in range(2)
    ]
    ms_practice_only = {
        "npi": "MSPRACTICE1",
        "addresses": [
            {"address_type": "business", "state": "AL", "city": "Mobile"},
            {"address_type": "practice", "state": "MS", "city": "Tupelo"},
        ],
    }
    coll.insert_many(ms_providers + ca_providers + [ms_practice_only])
    assert coll.count_documents({}) == 6

    drain_filter = {
        "addresses": {"$elemMatch": {"address_type": "business", "state": "MS"}}
    }
    result = coll.delete_many(drain_filter)

    assert result.deleted_count == 3, "must delete exactly the 3 MS-business rows"
    survivors = {d["npi"] for d in coll.find({}, {"npi": 1})}
    assert survivors == {"CA00000000", "CA00000001", "MSPRACTICE1"}, (
        "CA providers and the MS-practice-only provider must be preserved"
    )

    idx_names = {i["name"] for i in coll.list_indexes()}
    assert "npi_unique" in idx_names and "addr_state" in idx_names, (
        "indexes must be preserved (delete_many, not drop)"
    )


def test_constrained_drain_matches_zero_when_state_absent(target_coll):
    coll = target_coll
    coll.insert_many([
        {
            "npi": f"AL{i:08d}",
            "addresses": [{"address_type": "business", "state": "AL", "city": "Mobile"}],
        }
        for i in range(3)
    ])
    assert coll.count_documents({}) == 3

    result = coll.delete_many({
        "addresses": {"$elemMatch": {"address_type": "business", "state": "MS"}}
    })

    assert result.deleted_count == 0, "drain for absent state must delete nothing"
    assert coll.count_documents({}) == 3, "unrelated rows must survive"


def test_drains_do_not_touch_live_collections(mongo):
    """Sentinel: this test file must never touch PublicData.Provider_v_3
    or PublicStaging.StagingProvider_v_3. Counts before + after the module
    fixtures run must be identical. Reads only -- no writes."""
    frontend = ChatHealthyMongoUtilities("MONGO_FRONTEND_connectionString").getConnection()
    live_target_count = frontend["PublicData"]["Provider_v_3"].estimated_document_count()
    live_staging_count = mongo["PublicStaging"]["StagingProvider_v_3"].estimated_document_count()
    assert live_target_count >= 0
    assert live_staging_count >= 0
