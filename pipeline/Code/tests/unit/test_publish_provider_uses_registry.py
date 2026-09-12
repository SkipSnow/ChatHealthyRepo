# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Registry-coupling contract for steps.publish_provider.

Proves the step reads its staging + loaded collection names from
PipelineDatasetRegistry (dataset_versions[] in
brain/machine_artifacts/content/pipeline_config.json) rather than any
hardcoded string. If a config drift shifts provider's staging_name or
public_data_name in the brain JSON, publish_provider MUST follow it.

Approach: run the step against a real cluster, with the registry
pointed at scratch collections carrying a per-run uuid. A hardcoded
name could coincidentally match a production one; it cannot
coincidentally match a uuid. The assertions read where the data
actually landed, so they cover both the names the step resolved and
what the server did with them.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass
class _Args:
    data_version: int = 3
    env_prefix: str = "dev"
    provider_collection: str | None = None


@dataclass
class _Manifest:
    run_id: str = "run-provider-registry"
    metrics: dict = None

    def __post_init__(self):
        if self.metrics is None:
            self.metrics = {}


class _Ctx:
    """Minimal StepContext double; publish_provider goes through
    PipelineRuntime(ctx) so we only need the surfaces PR reads."""

    def __init__(self, mongo, frontend):
        self.mongo_client = mongo
        self.blob_client = None
        self.notification_client = None
        self.catalog_cache = None
        self.catalog = None
        self.step_summaries = {}
        self.config = {}
        self.args = _Args()
        self.manifest = _Manifest()
        self.env_prefix = "dev"
        self.run_id = self.manifest.run_id
        self._frontend = frontend


def _registry(pipeline_mongo, cfg, data_version=3):
    from pipeline_dataset_registry import PipelineDatasetRegistry
    return PipelineDatasetRegistry(cfg, data_version, pipeline_mongo)


@pytest.fixture
def clusters(monkeypatch, scratch_mongo):
    """A real cluster and scratch collections.

    Three things are redirected, all of them names: the dataset config
    the runtime loads, and the database and collection mark_loaded
    writes its metadata into. Nothing about MongoDB is stood in for —
    the server performs the rename, applies the upsert, and enforces the
    identity that reached it. Redirecting the destinations is what keeps
    a test run out of production data.

    The registry names both collections in one database because
    renameCollection is a same-database operation — the constraint the
    production layout is built around.
    """
    db, collection = scratch_mongo
    pipeline = db.client
    frontend = db.client

    staging = collection("provider_staging")
    public = collection("provider_loaded")
    cfg = {
        "dataset_versions": [
            {
                "source_name": "provider",
                "fetch": {"source_url": "https://example.com/provider.csv"},
                "file_format": "csv",
                "staging_name": f"{db.name}.{staging.name}",
                "public_data_name": f"{db.name}.{public.name}",
            },
        ],
    }

    import pipeline_loaded_metadata
    import pipeline_runtime

    monkeypatch.setattr(pipeline_runtime, "get_frontend_mongo", lambda: frontend)
    monkeypatch.setattr(pipeline_runtime, "get_mongo", lambda *_: pipeline)
    monkeypatch.setattr(pipeline_runtime, "load_pipeline_config", lambda **kw: cfg)
    monkeypatch.setattr(pipeline_loaded_metadata, "_METADATA_DB", db.name)
    monkeypatch.setattr(pipeline_loaded_metadata, "_METADATA_COLL",
                        collection("loaded_metadata").name)

    return pipeline, frontend, cfg


@pytest.mark.unit
def test_publish_provider_uses_registry_resolved_names(clusters):
    """publish_provider.execute MUST rename from the registry's
    staging_db.staging_coll_v_N to public_data_db.public_data_coll_v_N."""
    from steps.publish_provider import execute

    pipeline, frontend, cfg = clusters
    reg = _registry(pipeline, cfg, data_version=3)
    provider_entry = reg.by_source_name("provider")
    expected_src = f"{provider_entry.staging_db}.{reg.staging_collection_name('provider')}"
    expected_dst = f"{provider_entry.public_data_db}.{reg.public_data_collection_name('provider')}"

    src_db, src_coll = expected_src.split(".", 1)
    dst_db, dst_coll = expected_dst.split(".", 1)

    # Seed one row into the resolved staging collection so the count +
    # rename have something to operate on.
    pipeline[src_db][src_coll].insert_one({"npi": "1234567890", "run_id": "run-provider-registry"})
    # And a prior loaded collection at the destination, so dropTarget has
    # something to drop. Asserting the sentinel is gone afterwards proves
    # what dropTarget did, where reading it back off the command string
    # would only prove it was asked for.
    pipeline[dst_db][dst_coll].insert_one({"npi": "0000000000", "sentinel": "prior-fire"})

    ctx = _Ctx(pipeline, frontend)
    summary = execute(ctx)

    # The rename landed exactly where the registry said, carrying the
    # staged row and nothing from the prior fire.
    loaded = list(pipeline[dst_db][dst_coll].find({}))
    assert len(loaded) == 1
    assert loaded[0]["npi"] == "1234567890"
    assert src_coll not in pipeline[src_db].list_collection_names()
    assert summary["publichealthdata_collection_name"] == reg.public_data_collection_name("provider")
    assert summary["staging_collection_name"] == reg.staging_collection_name("provider")


@pytest.mark.unit
def test_publish_provider_bails_when_staging_absent(clusters):
    """If the registry-resolved staging collection does not exist on
    the pipeline cluster, publish_provider raises the well-named
    publish_provider_staging_missing exception -- no silent no-op."""
    from chathealthy_lib.exceptions import ChatHealthyException
    from steps.publish_provider import execute

    pipeline, frontend, _cfg = clusters
    ctx = _Ctx(pipeline, frontend)
    with pytest.raises(ChatHealthyException) as exc:
        execute(ctx)
    assert exc.value.mode == "publish_provider_staging_missing"
