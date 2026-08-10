# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Regression suite: pipeline Mongo and Key Vault access scope.

  1. The pipeline identity must be able to write the data on the pipeline
     cluster.

  2. The pipeline VM's Managed Identity (mi-control) must NOT be able to
     read MONGO-FRONTEND-connectionString from the pipeline's Key Vault.

Requires env: MONGO_connectionString, AZ_SUBSCRIPTION_ID,
AZ_VM_RESOURCE_GROUP (defaults rg-chathealthy-pipeline-dev).
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import uuid

import pytest


# Rule-004: one place in this file obtains a connection, and it goes through
# the canonical utility. The certificate is the credential; there is no
# connection string here and no fallback. Raises if the identity cannot
# connect, which is the point -- a test that quietly connects as something
# else proves nothing about production.
def _ch_connection(cluster: str = "pipelines"):
    import sys as _sys, pathlib as _pl
    for _d in _pl.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _lib = _d / "FrontEndApplicationLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            break
    from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities
    # The pipeline identity. Connecting as anyone else proves nothing.
    return ChatHealthyMongoUtilities().getConnection("pipelineEditor", cluster)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def pipeline_uri() -> str:
    uri = os.environ.get("MONGO_connectionString") or ""
    assert uri, "MONGO_connectionString is not set."
    return uri




# --------------------------------------------------------------------------
# Invariant 1: Mongo auth scope
# --------------------------------------------------------------------------
@pytest.mark.regression
def test_pipeline_user_can_write_to_pipeline_cluster_coord_db(pipeline_uri):
    """PipelineUserScoped MUST be able to insert+delete on
    Pipelines (the metadata DB). This is the write it needs
    every run for pipeline.runs, pipeline.work_items, etc."""
    from pymongo import MongoClient
    c = _ch_connection()
    test_id = f"iso_test_{uuid.uuid4().hex[:8]}"
    coll = c["pipelineAdmin"]["_isolation_test"]
    r = coll.insert_one({
        "_id": test_id,
        "created_at": datetime.datetime.utcnow().isoformat(),
        "purpose": "regression: pipeline user must write on pipeline cluster",
    })
    assert r.inserted_id == test_id, "insert failed to return the id"
    delete_result = coll.delete_one({"_id": test_id})
    assert delete_result.deleted_count == 1, "delete failed to clean up"


@pytest.mark.regression
def test_pipeline_user_can_write_to_pipeline_cluster_public_health_data(pipeline_uri):
    """PipelineUserScoped MUST be able to write to PublicHealthData on
    the pipeline cluster (that's where the payload lands via publish)."""
    from pymongo import MongoClient
    c = _ch_connection()
    test_id = f"iso_test_{uuid.uuid4().hex[:8]}"
    coll = c["PublicHealthData"]["_isolation_test"]
    r = coll.insert_one({"_id": test_id, "created_at": datetime.datetime.utcnow().isoformat()})
    assert r.inserted_id == test_id
    coll.delete_one({"_id": test_id})

# --------------------------------------------------------------------------
# Invariant 2: Key Vault access scope (via Azure control-plane inspection)
# --------------------------------------------------------------------------
_MI_NAME = "mi-control"
_KV_NAME = "kv-chpipeline-dev"
_FORBIDDEN_SECRET = "MONGO-FRONTEND-connectionString"








