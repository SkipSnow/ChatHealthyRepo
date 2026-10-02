# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Registry-coupling contract for pipeline_loaded_metadata.

should_skip and publichealthdata_collection_exists MUST read the loaded
DB name from PipelineDatasetRegistry.by_source_name(source_name).
public_data_db -- no module-level _LOADED_DB constant lives on this
module anymore. Each dataset_versions[] entry owns its own destination
DB and the skip gate walks that per-source, not a hardcode.

The gate's four conditions are claims about a real cluster: that a
collection exists, that its row count matches what was recorded. Both
are answered by the server, so these run against it, with the metadata
destination and the registry's collection names redirected to scratch
names so a test run never reads or writes production data.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def metadata_env(scratch_mongo, loaded_metadata_redirect):
    """A real cluster with the metadata destination redirected to scratch.

    pipeline_loaded_metadata addresses pipelineAdmin["pipeline.loaded_metadata"]
    on the front-end handle it is given. The handle passed here maps that
    one collection to a scratch collection, which is what keeps the test
    out of the live metadata collection; everything else -- the reads, the
    counts, the identity that reached the server -- is real.

    Yields (client, db_name, versioned_loaded_name, public_data_name,
    frontend, metadata_coll).
    """
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

    db, collection = scratch_mongo
    loaded = collection("SpecialtyMetaData")
    metadata_coll = collection("loaded_metadata")
    client = ChatHealthyMongoUtilities().getConnection("DevOpsUser", "ChatHealthyDataPipelines")

    return (
        client,
        db.name,
        f"{loaded.name}_v_3",
        f"{db.name}.{loaded.name}",
        loaded_metadata_redirect(client, metadata_coll),
        metadata_coll,
    )


def _registry(pipeline_mongo, public_data_name: str):
    from pipeline.run_lifecycle.pipeline_dataset_registry import PipelineDatasetRegistry
    cfg = {
        "dataset_versions": [
            {
                "source_name": "smd",
                "fetch": {"source_url": "https://example.com/smd.csv"},
                "file_format": "csv",
                "staging_name": f"{public_data_name}_staging",
                "public_data_name": public_data_name,
            },
        ],
    }
    return PipelineDatasetRegistry(cfg, 3, pipeline_mongo)


def _seed_metadata(metadata_coll, loaded_name, **fields):
    metadata_coll.insert_one({"_id": loaded_name, **fields})


@pytest.mark.unit
def test_module_no_longer_carries_loaded_db_constant():
    import pipeline.run_lifecycle.pipeline_loaded_metadata as mod
    assert not hasattr(mod, "_LOADED_DB"), (
        "pipeline_loaded_metadata._LOADED_DB MUST be gone -- the loaded DB "
        "name comes from PipelineDatasetRegistry.by_source_name(source_name)"
        ".public_data_db per dataset_versions[] entry."
    )


@pytest.mark.unit
def test_publichealthdata_collection_exists_uses_registry_public_data_db(metadata_env):
    from pipeline.run_lifecycle.pipeline_loaded_metadata import publichealthdata_collection_exists

    client, db_name, loaded_name, public_data_name, _, _ = metadata_env
    reg = _registry(client, public_data_name)

    client[db_name][loaded_name].insert_one({"_id": 1})

    assert publichealthdata_collection_exists(
        client, reg, "smd", loaded_name,
    ) is True
    assert publichealthdata_collection_exists(
        client, reg, "smd", f"{loaded_name}_NonExistent",
    ) is False


@pytest.mark.unit
def test_should_skip_reads_row_count_from_registry_public_data_db(metadata_env):
    from pipeline.run_lifecycle.pipeline_loaded_metadata import should_skip

    client, db_name, loaded_name, public_data_name, frontend, metadata_coll = metadata_env
    reg = _registry(client, public_data_name)

    _seed_metadata(metadata_coll, loaded_name,
                   source_hash="abc123", operationally_fit=True, row_count=2)
    client[db_name][loaded_name].insert_many([{"_id": 1}, {"_id": 2}])

    skip, reason = should_skip(
        pipeline_mongo=client,
        frontend_mongo=frontend,
        registry=reg,
        source_name="smd",
        publichealthdata_collection_name=loaded_name,
        current_source_hash="abc123",
    )
    assert skip is True
    assert "operationally_fit=True" in reason


@pytest.mark.unit
def test_should_skip_forces_reload_when_registry_db_row_count_drifts(metadata_env):
    from pipeline.run_lifecycle.pipeline_loaded_metadata import should_skip

    client, db_name, loaded_name, public_data_name, frontend, metadata_coll = metadata_env
    reg = _registry(client, public_data_name)

    # Metadata says 5 rows; only 2 are present -> parity mismatch.
    _seed_metadata(metadata_coll, loaded_name,
                   source_hash="abc123", operationally_fit=True, row_count=5)
    client[db_name][loaded_name].insert_many([{"_id": 1}, {"_id": 2}])

    skip, reason = should_skip(
        pipeline_mongo=client,
        frontend_mongo=frontend,
        registry=reg,
        source_name="smd",
        publichealthdata_collection_name=loaded_name,
        current_source_hash="abc123",
    )
    assert skip is False
    assert "row-count parity mismatch" in reason


@pytest.mark.unit
def test_should_skip_forces_reload_when_collection_absent_on_registry_db(metadata_env):
    from pipeline.run_lifecycle.pipeline_loaded_metadata import should_skip

    client, db_name, loaded_name, public_data_name, frontend, metadata_coll = metadata_env
    reg = _registry(client, public_data_name)

    _seed_metadata(metadata_coll, loaded_name,
                   source_hash="abc123", operationally_fit=True, row_count=0)
    # The loaded collection is never created.
    skip, reason = should_skip(
        pipeline_mongo=client,
        frontend_mongo=frontend,
        registry=reg,
        source_name="smd",
        publichealthdata_collection_name=loaded_name,
        current_source_hash="abc123",
    )
    assert skip is False
    assert "does not exist on pipeline cluster" in reason
