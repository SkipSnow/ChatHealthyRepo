# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pytest for EPIC-008-F-004-S-001-REQ-T-002: post-commit hook seeds and increments
# the version doc in MongoDB at {ENV_PREFIX}_System.version.
#
# Tests the same seed/increment logic that lives in .git/hooks/post-commit.
# Uses a sandbox _id so the live _id='current' banner doc is never touched.
# Rolls back at the end (deletes the sandbox doc).

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument

REPO_ROOT = Path(__file__).resolve().parents[5]
VERSION_JSON = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "version.json"
ENV_FILE = REPO_ROOT / "Code" / ".env"
SANDBOX_ID = "pytest_post_commit_seed"


def _load_env():
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)


def _read_version_from_disk():
    """Same extraction the post-commit hook performs (new schema shape)."""
    with open(VERSION_JSON, encoding="utf-8") as f:
        data = json.load(f)
    latest_version = data["versions"]["version"][-1]
    latest_build = latest_version["builds"]["build"][-1]
    return {
        "version": latest_version["version_number"],
        "framework": latest_version["framework_version"],
        "build": latest_build["build_number"],
    }


@pytest.fixture(scope="module")
def mongo_collection():
    _load_env()
    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        pytest.skip("MONGO_FRONTEND_connectionString not set in environment")
    env_prefix = os.getenv("ENV_PREFIX", "dev")
    client = MongoClient(conn, serverSelectionTimeoutMS=8000)
    coll = client[f"{env_prefix}_System"]["version"]
    coll.delete_one({"_id": SANDBOX_ID})  # ensure clean slate
    yield coll
    coll.delete_one({"_id": SANDBOX_ID})  # rollback


def _seed(coll, expected):
    """Same upsert the post-commit hook performs on first run."""
    coll.update_one(
        {"_id": SANDBOX_ID},
        {
            "$set": {
                "version": expected["version"],
                "framework": expected["framework"],
            },
            "$setOnInsert": {"build": expected["build"]},
        },
        upsert=True,
    )


def _increment(coll):
    """Same $inc the post-commit hook performs on every commit."""
    return coll.find_one_and_update(
        {"_id": SANDBOX_ID},
        {"$inc": {"build": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


class TestPostCommitSeed:
    """EPIC-008-F-004-S-001-REQ-T-002: hook seeds + increments version doc correctly."""

    def test_extract_from_version_json_uses_new_schema_shape(self):
        """Seed extraction reads versions.version[-1].* (new shape), not current.*."""
        extracted = _read_version_from_disk()
        assert extracted["version"], "version_number must be non-empty"
        assert extracted["framework"], "framework_version must be non-empty"
        assert isinstance(extracted["build"], int), "build_number must be int"
        assert extracted["build"] >= 1, "build_number must be positive"

    def test_seed_writes_correct_record(self, mongo_collection):
        """Seed writes version + framework from version.json; build from version.json."""
        expected = _read_version_from_disk()
        _seed(mongo_collection, expected)
        doc = mongo_collection.find_one({"_id": SANDBOX_ID})
        assert doc is not None, "seed must create the doc"
        assert doc["version"] == expected["version"]
        assert doc["framework"] == expected["framework"]
        assert doc["build"] == expected["build"]

    def test_increment_increases_build_by_one(self, mongo_collection):
        """$inc must increment build by exactly 1 each call (REQ-016 'exactly one')."""
        before = mongo_collection.find_one({"_id": SANDBOX_ID})["build"]
        after_doc = _increment(mongo_collection)
        assert after_doc["build"] == before + 1, "build must increment by exactly 1"
        # Idempotent shape: version + framework still present after increment.
        assert "version" in after_doc and "framework" in after_doc

    def test_increment_preserves_version_and_framework(self, mongo_collection):
        """REQ-016: deploy/automation MUST NOT change version or framework fields."""
        expected = _read_version_from_disk()
        _increment(mongo_collection)
        doc = mongo_collection.find_one({"_id": SANDBOX_ID})
        assert doc["version"] == expected["version"], "increment must not change version"
        assert doc["framework"] == expected["framework"], "increment must not change framework"
