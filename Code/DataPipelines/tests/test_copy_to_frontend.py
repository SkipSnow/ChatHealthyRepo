# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""CopyToFrontEnd parity tests — PIPE-CP-001"""
import os, sys, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

def _get_clients():
    from pymongo import MongoClient
    pipeline_uri = os.environ.get("MONGO_connectionString")
    frontend_uri = os.environ.get("MONGO_FRONTEND_connectionString")
    if not pipeline_uri or not frontend_uri:
        pytest.skip("MongoDB connection strings not set")
    return MongoClient(pipeline_uri), MongoClient(frontend_uri)

def test_parity_after_copy():
    """EPIC-006-F-016-S-001-REQ-B-001: source count == destination count for copied collections"""
    pipeline_client, frontend_client = _get_clients()
    env = os.environ.get("ENV_PREFIX", "dev")
    db_name = f"{env}_PublicHealthData"

    collections_to_check = ["SpecialtyMetaData", "provider_quality"]
    for coll_name in collections_to_check:
        src = pipeline_client[db_name][coll_name].count_documents({})
        dst = frontend_client[db_name][coll_name].count_documents({})
        if src == 0:
            continue  # nothing to verify if source is empty
        assert src == dst, f"Parity failure {coll_name}: pipeline={src} frontend={dst}"

def test_parity_per_state():
    """EPIC-006-F-016-S-001-REQ-B-002: provider count matches per state"""
    pipeline_client, frontend_client = _get_clients()
    env = os.environ.get("ENV_PREFIX", "dev")
    db_name = f"{env}_PublicHealthData"

    states = ["DE", "MS", "VA"]
    for state in states:
        q = {"practice_address.state": state}
        src = pipeline_client[db_name]["providers"].count_documents(q)
        dst = frontend_client[db_name]["providers"].count_documents(q)
        if src == 0:
            continue
        assert src == dst, f"Parity failure providers({state}): pipeline={src} frontend={dst}"
