# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Registry-coupling contract for pipeline_loaded_metadata.

should_skip and publichealthdata_collection_exists MUST read the loaded
DB name from PipelineDatasetRegistry.by_source_name(source_name).
public_data_db -- no module-level _LOADED_DB constant lives on this
module anymore. Each dataset_versions[] entry owns its own destination
DB and the skip gate walks that per-source, not a hardcode.
"""

from __future__ import annotations

import mongomock
import pytest

# All pipeline metadata lives in one database. This runbook ships to Azure
# Automation standalone, so it carries the constant rather than importing
# pipeline_db, which is not deployed alongside it.
PIPELINE_ADMIN_DB = "pipelineAdmin"



def _registry(pipeline_mongo, public_data_db: str = "PublicHealthData"):
    from pipeline_dataset_registry import PipelineDatasetRegistry
    cfg = {
        "dataset_versions": [
            {
                "source_name": "smd",
                "fetch": {"source_url": "https://example.com/smd.csv"},
                "file_format": "csv",
                "staging_name": "PublicStaging.SpecialtyMetaData_staging",
                "public_data_name": f"{public_data_db}.SpecialtyMetaData",
            },
        ],
    }
    return PipelineDatasetRegistry(cfg, 3, pipeline_mongo)


@pytest.mark.unit
def test_module_no_longer_carries_loaded_db_constant():
    import pipeline_loaded_metadata as mod
    assert not hasattr(mod, "_LOADED_DB"), (
        "pipeline_loaded_metadata._LOADED_DB MUST be gone -- the loaded DB "
        "name comes from PipelineDatasetRegistry.by_source_name(source_name)"
        ".public_data_db per dataset_versions[] entry."
    )


@pytest.mark.unit
def test_publichealthdata_collection_exists_uses_registry_public_data_db():
    from pipeline_loaded_metadata import publichealthdata_collection_exists
    pipeline = mongomock.MongoClient()
    reg = _registry(pipeline, public_data_db="PublicHealthData")
    # Seed a doc in the registry-resolved DB.
    pipeline["PublicHealthData"]["SpecialtyMetaData_v_3"].insert_one({"_id": 1})
    # Confirmation: same collection name under a different DB does NOT count.
    pipeline["SomeOtherDb"]["SpecialtyMetaData_v_3"].insert_one({"_id": 1})
    assert publichealthdata_collection_exists(
        pipeline, reg, "smd", "SpecialtyMetaData_v_3",
    ) is True
    assert publichealthdata_collection_exists(
        pipeline, reg, "smd", "NonExistent_v_3",
    ) is False


@pytest.mark.unit
def test_should_skip_reads_row_count_from_registry_public_data_db():
    from pipeline_loaded_metadata import should_skip
    pipeline = mongomock.MongoClient()
    frontend = mongomock.MongoClient()
    reg = _registry(pipeline, public_data_db="PublicHealthData")

    # Prior successful load metadata on the frontend cluster.
    frontend[PIPELINE_ADMIN_DB]["pipeline.loaded_metadata"].insert_one({
        "_id": "SpecialtyMetaData_v_3",
        "source_hash": "abc123",
        "operationally_fit": True,
        "row_count": 2,
    })
    # Physical rows on the registry-resolved (PublicHealthData) DB.
    pipeline["PublicHealthData"]["SpecialtyMetaData_v_3"].insert_many([
        {"_id": 1}, {"_id": 2},
    ])

    skip, reason = should_skip(
        pipeline_mongo=pipeline,
        frontend_mongo=frontend,
        registry=reg,
        source_name="smd",
        publichealthdata_collection_name="SpecialtyMetaData_v_3",
        current_source_hash="abc123",
    )
    assert skip is True
    assert "operationally_fit=True" in reason


@pytest.mark.unit
def test_should_skip_forces_reload_when_registry_db_row_count_drifts():
    from pipeline_loaded_metadata import should_skip
    pipeline = mongomock.MongoClient()
    frontend = mongomock.MongoClient()
    reg = _registry(pipeline, public_data_db="PublicHealthData")

    frontend[PIPELINE_ADMIN_DB]["pipeline.loaded_metadata"].insert_one({
        "_id": "SpecialtyMetaData_v_3",
        "source_hash": "abc123",
        "operationally_fit": True,
        "row_count": 5,  # says 5, but only 2 present -> parity mismatch
    })
    pipeline["PublicHealthData"]["SpecialtyMetaData_v_3"].insert_many([
        {"_id": 1}, {"_id": 2},
    ])

    skip, reason = should_skip(
        pipeline_mongo=pipeline,
        frontend_mongo=frontend,
        registry=reg,
        source_name="smd",
        publichealthdata_collection_name="SpecialtyMetaData_v_3",
        current_source_hash="abc123",
    )
    assert skip is False
    assert "row-count parity mismatch" in reason


@pytest.mark.unit
def test_should_skip_forces_reload_when_collection_absent_on_registry_db():
    from pipeline_loaded_metadata import should_skip
    pipeline = mongomock.MongoClient()
    frontend = mongomock.MongoClient()
    reg = _registry(pipeline, public_data_db="PublicHealthData")

    frontend[PIPELINE_ADMIN_DB]["pipeline.loaded_metadata"].insert_one({
        "_id": "SpecialtyMetaData_v_3",
        "source_hash": "abc123",
        "operationally_fit": True,
        "row_count": 0,
    })
    # Collection absent on registry-resolved DB.
    skip, reason = should_skip(
        pipeline_mongo=pipeline,
        frontend_mongo=frontend,
        registry=reg,
        source_name="smd",
        publichealthdata_collection_name="SpecialtyMetaData_v_3",
        current_source_hash="abc123",
    )
    assert skip is False
    assert "does not exist on pipeline cluster" in reason
