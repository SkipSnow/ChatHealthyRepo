# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pytest: Manifest completeness — DR-021
# Verifies that MongoDB collections are tracked in the project manifest
# or traceability matrix, and that manifest files are valid JSON.
#
# Run: pytest Code/DataPipelines/tests/test_manifest.py -v

import json
import os

import pytest

BRAIN_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "brain",
                 "machine_artifacts", "content")
)
MANIFEST_PATH = os.path.join(BRAIN_DIR, "project_manifest.json")
MATRIX_PATH = os.path.join(BRAIN_DIR, "traceability_matrix.json")


def _load_json(path):
    """Load and return parsed JSON from *path*."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _get_mongo_client():
    """Return a pymongo MongoClient for the frontend cluster.

    Skips the test if the connection string is not available or if
    pymongo is not installed.
    """
    try:
        import pymongo  # noqa: F811
    except ImportError:
        pytest.skip("pymongo not installed — skipping MongoDB tests")

    conn_str = os.environ.get("FRONTEND_MONGO_URI") or os.environ.get(
        "MONGO_URI_FRONTEND"
    )
    if not conn_str:
        pytest.skip(
            "No FRONTEND_MONGO_URI or MONGO_URI_FRONTEND env var set — "
            "skipping MongoDB tests"
        )
    client = pymongo.MongoClient(conn_str, serverSelectionTimeoutMS=5000)
    # Quick connectivity check
    try:
        client.admin.command("ping")
    except Exception as exc:
        pytest.skip(f"Cannot reach MongoDB frontend cluster: {exc}")
    return client


# ------------------------------------------------------------------
# test_manifest_valid_json
# ------------------------------------------------------------------
def test_manifest_valid_json():
    """Verify that project_manifest.json parses as valid JSON."""
    data = _load_json(MANIFEST_PATH)
    assert isinstance(data, dict), "Top-level value must be a JSON object"


# ------------------------------------------------------------------
# test_mongodb_collections_in_manifest
# ------------------------------------------------------------------
def test_mongodb_collections_in_manifest():
    """Every user-created collection on the frontend cluster must appear
    in project_manifest.json or traceability_matrix.json."""
    client = _get_mongo_client()
    manifest = _load_json(MANIFEST_PATH)
    matrix = _load_json(MATRIX_PATH)

    # Flatten all string values from manifest and matrix into a single
    # searchable blob so we can detect collection name references
    # regardless of where they appear in the structure.
    manifest_text = json.dumps(manifest)
    matrix_text = json.dumps(matrix)
    combined_text = manifest_text + matrix_text

    # System databases and collections to ignore
    system_dbs = {"admin", "local", "config"}

    missing = []
    for db_name in client.list_database_names():
        if db_name in system_dbs:
            continue
        db = client[db_name]
        for coll_name in db.list_collection_names():
            if coll_name.startswith("system."):
                continue
            if coll_name not in combined_text:
                missing.append(f"{db_name}.{coll_name}")

    assert not missing, (
        f"MongoDB collections not referenced in manifest or matrix: "
        f"{missing}"
    )
