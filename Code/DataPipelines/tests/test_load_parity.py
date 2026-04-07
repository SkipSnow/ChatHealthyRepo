# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pytest: Load parity check — source file count must match DB count.
# For every state in the database, the number of records in the raw
# NPPES CSV (blob storage) must equal the number in the pipeline
# cluster's providers collection. If any state is short, the test
# fails and reports which states are missing records and how many.
#
# BUG-LOAD-001: This test would have caught the CopyToFrontEnd bug.
#
# Requires: MONGO_connectionString (pipeline cluster) + AZURE_STORAGE_CONNECTION_STRING
# Run: pytest Code/DataPipelines/tests/test_load_parity.py -v -s

import csv
import io
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_log = logging.getLogger("test_load_parity")


def _get_pipeline_connection():
    val = os.environ.get("MONGO_connectionString")
    if not val:
        pytest.skip("No MONGO_connectionString (pipeline cluster) in env")
    return val


def _get_blob_service():
    if not os.environ.get("AZURE_STORAGE_CONNECTION_STRING"):
        pytest.skip("No AZURE_STORAGE_CONNECTION_STRING in env")
    from blob_client import get_blob_service
    return get_blob_service()


@pytest.fixture(scope="module")
def pipeline_providers():
    """Connect to pipeline cluster providers collection."""
    from pymongo import MongoClient
    client = MongoClient(_get_pipeline_connection())
    return client[f"{os.environ.get('ENV_PREFIX', 'dev')}_PublicHealthData"]["providers"]


@pytest.fixture(scope="module")
def db_state_counts(pipeline_providers):
    """Count providers per state in the pipeline cluster."""
    counts = {}
    for doc in pipeline_providers.aggregate([
        {"$group": {"_id": "$practice_address.state", "count": {"$sum": 1}}},
    ]):
        state = doc["_id"]
        if state:
            counts[state] = doc["count"]
    return counts


@pytest.fixture(scope="module")
def source_state_counts():
    """Count providers per state in the raw NPPES CSV in blob storage."""
    service = _get_blob_service()
    container = service.get_container_client("provider-data")

    # Find the NPPES CSV blob
    csv_blob = None
    for blob in container.list_blobs():
        if blob.name.endswith(".csv") and "npi" in blob.name.lower():
            csv_blob = blob.name
            break

    if not csv_blob:
        pytest.skip("No NPPES CSV found in blob storage")

    _log.info("Counting source records from blob: %s", csv_blob)
    blob_client = container.get_blob_client(csv_blob)
    stream = blob_client.download_blob()

    counts = {}
    total = 0

    # Use csv.DictReader for proper handling of quoted fields with commas
    import csv
    import io

    chunk_buffer = ""
    for chunk in stream.chunks():
        chunk_buffer += chunk.decode("utf-8", errors="replace")

    reader = csv.DictReader(io.StringIO(chunk_buffer))

    # Find the practice state column
    state_col = None
    for col in (reader.fieldnames or []):
        col_lower = col.strip().lower()
        if "practice" in col_lower and "state" in col_lower and "name" in col_lower:
            state_col = col
            break

    if state_col is None:
        pytest.fail(f"No practice state column in CSV: {(reader.fieldnames or [])[:20]}")

    _log.info("Using state column: %s", state_col)

    for row in reader:
        total += 1
        state = (row.get(state_col) or "").strip().upper()
        if state and len(state) == 2:
            counts[state] = counts.get(state, 0) + 1

        if total % 2_000_000 == 0:
            _log.info("  %dM rows scanned...", total // 1_000_000)

    _log.info("Source scan complete: %d total rows, %d states", total, len(counts))
    return counts


class TestLoadParity:
    """Source file record count must match pipeline DB record count per state."""

    def test_parity_for_loaded_states(self, db_state_counts, source_state_counts):
        """For every state in the DB, source count must equal DB count."""
        mismatches = []

        for state, db_count in sorted(db_state_counts.items()):
            source_count = source_state_counts.get(state, 0)
            if source_count != db_count:
                diff = source_count - db_count
                mismatches.append({
                    "state": state,
                    "source": source_count,
                    "db": db_count,
                    "missing": diff,
                })

        if mismatches:
            report = "\n  LOAD PARITY FAILURES:\n"
            for m in mismatches:
                report += (
                    f"    {m['state']}: source={m['source']:,} "
                    f"db={m['db']:,} "
                    f"missing={m['missing']:,}\n"
                )
            print(report)

        assert len(mismatches) == 0, (
            f"{len(mismatches)} states have source/DB mismatch: "
            f"{', '.join(m['state'] for m in mismatches)}"
        )

    def test_no_empty_states_in_db(self, db_state_counts):
        """No state in the DB should have zero records."""
        empty = [s for s, c in db_state_counts.items() if c == 0]
        assert len(empty) == 0, f"States with 0 records: {empty}"

    def test_db_has_loaded_states(self, db_state_counts):
        """DB must have at least one state loaded."""
        assert len(db_state_counts) > 0, "No states found in pipeline DB"
        print(f"\n  States in DB: {sorted(db_state_counts.keys())}")
        for state, count in sorted(db_state_counts.items()):
            print(f"    {state}: {count:,}")


class TestFrontendParity:
    """Frontend cluster must match pipeline cluster for all loaded states."""

    def test_frontend_matches_pipeline(self, db_state_counts):
        """Frontend cluster record count must equal pipeline cluster per state."""
        frontend_conn = os.environ.get(
            "MONGO_READONLY_FRONTEND",
            os.environ.get("MONGO_FRONTEND_connectionString"),
        )
        if not frontend_conn:
            pytest.skip("No frontend connection string in env")

        from pymongo import MongoClient
        fe_coll = MongoClient(frontend_conn)[f"{os.environ.get('ENV_PREFIX', 'dev')}_PublicHealthData"]["providers"]

        mismatches = []
        for state, pipeline_count in sorted(db_state_counts.items()):
            fe_count = fe_coll.count_documents({"practice_address.state": state})
            if fe_count != pipeline_count:
                mismatches.append({
                    "state": state,
                    "pipeline": pipeline_count,
                    "frontend": fe_count,
                    "missing": pipeline_count - fe_count,
                })

        if mismatches:
            report = "\n  FRONTEND PARITY FAILURES:\n"
            for m in mismatches:
                report += (
                    f"    {m['state']}: pipeline={m['pipeline']:,} "
                    f"frontend={m['frontend']:,} "
                    f"missing={m['missing']:,}\n"
                )
            print(report)

        assert len(mismatches) == 0, (
            f"{len(mismatches)} states have pipeline/frontend mismatch: "
            f"{', '.join(m['state'] for m in mismatches)}"
        )
