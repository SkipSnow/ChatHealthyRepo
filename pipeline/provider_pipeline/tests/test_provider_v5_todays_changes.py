# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Integration test: confirm the 2026-09-28 provider-record changes on the
served provider collection. Runs in-cluster (pipelineEditor) via the
provider_regression_test runbook -- it reads the real published collection, so
it cannot run from a developer workstation, which has no path to the pipeline
cluster.

Confirms four things shipped today:
  1. Active standing -- every provider carries an `active.is_active` four-value
     flag (Default_true / history_true / original_false / history_false), always
     present, never the old event-log array.
  2. Coordinate invariant -- every eligible practice address carries a
     coordinates block that is EITHER the -1 pending sentinel OR a resolved
     point (latitude/longitude not -1, source census_batch|google_maps). Never
     absent.
  3. Index set -- the collection holds exactly the target indexes, including
     active.is_active_1 and practice_addresses.coordinates.source_1, and no
     illegal parallel-array index.
  4. Staging emptied -- PublicStaging holds no collections after the successful
     run.
"""
from __future__ import annotations

import pytest

from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
from pipeline.run_lifecycle.pipeline_config import load_pipeline_config
from pipeline.run_lifecycle.pipeline_dataset_registry import PipelineDatasetRegistry

_ACTIVE_VALUES = {"Default_true", "history_true", "original_false", "history_false"}
_PRACTICE_TYPES = {"practice", "secondary_practice"}
_REAL_SOURCES = {"census_batch", "google_maps"}
_TARGET_INDEXES = {
    "_id_", "npi_1", "business_state_taxonomy",
    "practice_addresses.county.source_1", "entity_taxonomy",
    "entity_practice_state", "practice_addresses.coordinates.source_1",
    "active.is_active_1",
}


def _env_prefix():
    import os
    return os.environ.get("ENV_PREFIX", "dev")


@pytest.fixture(scope="module")
def provider_coll():
    import os
    cfg = load_pipeline_config(env_prefix=_env_prefix())
    data_version = int(os.environ.get("DATA_VERSION") or cfg.get("data_version") or 0)
    pipeline_mongo = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyDataPipelines")
    registry = PipelineDatasetRegistry(cfg, data_version, pipeline_mongo)
    entry = registry.by_source_name("provider")
    db, coll = entry.public_data_db, registry.public_data_collection_name("provider")
    return pipeline_mongo[db][coll]


def test_collection_not_empty(provider_coll):
    assert provider_coll.count_documents({}) > 0, "served provider collection is empty"


def test_active_flag_always_present_and_valid(provider_coll):
    bad = list(provider_coll.find(
        {"$or": [
            {"active": {"$exists": False}},
            {"active.is_active": {"$nin": list(_ACTIVE_VALUES)}},
        ]},
        {"npi": 1, "active": 1}).limit(20))
    assert not bad, f"providers with missing/invalid active.is_active: {bad}"


def test_coordinate_invariant(provider_coll):
    # Every eligible practice address is either the -1 sentinel or a real point.
    offenders = []
    for doc in provider_coll.find({}, {"npi": 1, "practice_addresses": 1}):
        for addr in (doc.get("practice_addresses") or []):
            if addr.get("address_type") not in _PRACTICE_TYPES:
                continue
            c = addr.get("coordinates")
            if not isinstance(c, dict):
                offenders.append((doc.get("npi"), "no coordinates block"))
                break
            lat, lon, src = c.get("latitude"), c.get("longitude"), c.get("source")
            pending = (lat == -1 and lon == -1 and src == "pending")
            real = (lat not in (None, -1) and lon not in (None, -1) and src in _REAL_SOURCES)
            if not (pending or real):
                offenders.append((doc.get("npi"), c))
                break
        if len(offenders) >= 20:
            break
    assert not offenders, f"addresses neither -1 nor resolved: {offenders}"


def test_index_set_is_exact(provider_coll):
    names = {ix["name"] for ix in provider_coll.list_indexes()}
    missing = _TARGET_INDEXES - names
    extra = names - _TARGET_INDEXES
    assert not missing, f"missing indexes: {missing}"
    assert not extra, f"unexpected/stale indexes present: {extra}"


def test_staging_emptied_after_success():
    staging = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyDataPipelines")["PublicStaging"]
    left = staging.list_collection_names()
    assert not left, f"PublicStaging not empty after a successful run: {left}"
