# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Registry-coupling contract for steps.publish_provider.

Proves the step reads its staging + loaded collection names from
PipelineDatasetRegistry (dataset_versions[] in
brain/machine_artifacts/content/pipeline_config.json) rather than any
hardcoded string. If a config drift shifts provider's staging_name or
public_data_name in the brain JSON, publish_provider MUST follow it.

Approach: intercept the mongo cluster's admin.command and capture the
renameCollection args; assert both source and target strings compose
the registry's resolved (staging_db.staging_coll, public_data_db.
public_data_coll) exactly, versioned with the data_version the
runtime carries.
"""

from __future__ import annotations

from dataclasses import dataclass

import mongomock
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


def _registry(pipeline_mongo, data_version=3):
    from pipeline_config import load_pipeline_config
    from pipeline_dataset_registry import PipelineDatasetRegistry
    return PipelineDatasetRegistry(
        load_pipeline_config(env_prefix="dev"), data_version, pipeline_mongo,
    )


@pytest.fixture
def clusters(monkeypatch):
    pipeline = mongomock.MongoClient()
    frontend = mongomock.MongoClient()
    captured: dict = {}

    def _fake_admin(cmd, *args, **kwargs):
        if isinstance(cmd, dict) and "renameCollection" in cmd:
            captured["cmd"] = dict(cmd)
            src_db, src_coll = cmd["renameCollection"].split(".", 1)
            dst_db, dst_coll = cmd["to"].split(".", 1)
            if cmd.get("dropTarget"):
                pipeline[dst_db].drop_collection(dst_coll)
            for doc in pipeline[src_db][src_coll].find({}):
                pipeline[dst_db][dst_coll].insert_one(doc)
            pipeline[src_db].drop_collection(src_coll)
            return {"ok": 1}
        raise NotImplementedError(f"fake admin.command does not handle {cmd!r}")

    pipeline.admin.command = _fake_admin
    monkeypatch.setattr("pipeline_runtime.get_frontend_mongo", lambda: frontend)
    monkeypatch.setattr("pipeline_runtime.get_mongo", lambda: pipeline)
    return pipeline, frontend, captured


@pytest.mark.unit
def test_publish_provider_uses_registry_resolved_names(clusters):
    """publish_provider.execute MUST rename from the registry's
    staging_db.staging_coll_v_N to public_data_db.public_data_coll_v_N."""
    from steps.publish_provider import execute

    pipeline, frontend, captured = clusters
    reg = _registry(pipeline, data_version=3)
    provider_entry = reg.by_source_name("provider")
    expected_src = f"{provider_entry.staging_db}.{reg.staging_collection_name('provider')}"
    expected_dst = f"{provider_entry.public_data_db}.{reg.public_data_collection_name('provider')}"

    # Seed one row into the resolved staging collection so the count +
    # rename have something to operate on.
    src_db, src_coll = expected_src.split(".", 1)
    pipeline[src_db][src_coll].insert_one({"npi": "1234567890", "run_id": "run-provider-registry"})

    ctx = _Ctx(pipeline, frontend)
    summary = execute(ctx)

    assert captured["cmd"]["renameCollection"] == expected_src
    assert captured["cmd"]["to"] == expected_dst
    assert captured["cmd"]["dropTarget"] is True
    # Post-rename: loaded collection resolved from the registry holds the
    # seeded row; staging has been drained.
    dst_db, dst_coll = expected_dst.split(".", 1)
    assert pipeline[dst_db][dst_coll].count_documents({}) == 1
    assert src_coll not in pipeline[src_db].list_collection_names()
    assert summary["publichealthdata_collection_name"] == reg.public_data_collection_name("provider")
    assert summary["staging_collection_name"] == reg.staging_collection_name("provider")


@pytest.mark.unit
def test_publish_provider_bails_when_staging_absent(clusters):
    """If the registry-resolved staging collection does not exist on
    the pipeline cluster, publish_provider raises the well-named
    publish_provider_staging_missing exception -- no silent no-op."""
    from chathealthy_frontend_lib.exceptions import ChatHealthyException
    from steps.publish_provider import execute

    pipeline, frontend, _captured = clusters
    ctx = _Ctx(pipeline, frontend)
    with pytest.raises(ChatHealthyException) as exc:
        execute(ctx)
    assert exc.value.mode == "publish_provider_staging_missing"
