# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# EPIC-010-F-006-S-003-REQ-B-002 / REQ-T-003 — State-Scoped Drain
#
# End-to-end test of the state-scoped drain semantic.
#
# Flow (Skip 2026-05-05 spec):
#   A. Load 2 small states (DC + RI by default) into a uniquely-named test
#      collection. Embeddings off. Single env (dev_PublicHealthData test slot).
#   B. Capture record count for the "survivor" state (RI) — this is the count
#      that must NOT change.
#   C. Re-run the full load against the same test collection but with
#      states=[DRAIN_STATE only] (DC). Drain must remove only DC records;
#      survivor (RI) records must persist. Loader inserts DC fresh.
#   D. Assert survivor's record count is identical to the captured-in-B count.
#
# Test does NOT drop the test collections after — Skip directive 2026-05-05.
# Operator can inspect or drop manually:
#     test_DataPipelines.test_providers_<uuid>
#     test_DataPipelines.test_DataLoadMetadata_<uuid>
#
# Activities are called directly (not through the Durable Functions orchestrator).
# Same pattern as tests/test_load_parity.py — proven path, no Azure runtime needed.

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

# Make pipeline/Code importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from provider_load_manager import (  # noqa: E402
    drain_staging_fn,
    provider_worker_fn,
    download_zip_fn,
    extract_csv_fn,
    partition_file_fn,
    write_metadata_fn,
    ensure_postload_indexes_fn,
    _get_mongo_client,
)

# ── Test config ─────────────────────────────────────────────────────────────
RUN_ID = uuid.uuid4().hex[:8]
TEST_DB = "test_DataPipelines"
TEST_PROVIDERS_COLL = f"test_providers_{RUN_ID}"
TEST_METADATA_COLL = f"test_DataLoadMetadata_{RUN_ID}"
TEST_BLOB_CONTAINER = "test-pipeline-data"

PROVIDER_COLLECTION = f"{TEST_DB}.{TEST_PROVIDERS_COLL}"
METADATA_COLLECTION = f"{TEST_DB}.{TEST_METADATA_COLL}"

# Two small states. VT + WY were chosen 2026-05-05 from a live query against
# providers_enriched (8.86M docs): VT=16,899 NPIs, WY=17,117 NPIs.
# Override via env STATE_KEEP / STATE_DRAIN.
STATE_KEEP = os.environ.get("STATE_KEEP", "VT").upper()   # survivor — must persist through Step C
STATE_DRAIN = os.environ.get("STATE_DRAIN", "WY").upper()  # drained then reloaded in Step C

LOAD_ID_AB = f"test-AB-{RUN_ID}"
LOAD_ID_C = f"test-C-{RUN_ID}"


def _base_config(*, states: list[str], load_id: str) -> dict:
    return {
        "provider_collection": PROVIDER_COLLECTION,
        "metadata_collection": METADATA_COLLECTION,
        "blob_container":      TEST_BLOB_CONTAINER,
        "states":              states,
        "incremental":         False,
        "embedding_enabled":   False,
        "num_workers":         2,
        "batch_size":          500,
        "version":             f"test-{RUN_ID}",
        "load_id":             load_id,
    }


@pytest.fixture(scope="module")
def mongo():
    if not os.environ.get("MONGO_connectionString"):
        pytest.skip("MONGO_connectionString not set — required for end-to-end pipeline test")
    return _get_mongo_client()


@pytest.fixture(scope="module")
def csv_path():
    """Download + extract NPPES CSV once for the whole test run.

    Reuses the existing downloader/extractor — same code path the production
    pipeline uses. The result is a path to the CSV (on local disk inside the
    process) that the partition step + workers can iterate.
    """
    if not os.environ.get("AZURE_STORAGE_CONNECTION_STRING"):
        pytest.skip("AZURE_STORAGE_CONNECTION_STRING not set — required to fetch NPPES CSV")
    cfg = _base_config(states=[STATE_KEEP, STATE_DRAIN], load_id=LOAD_ID_AB)
    download_result = download_zip_fn(cfg)
    zip_path = download_result["zip_path"]
    csv_path = extract_csv_fn({**cfg, "zip_path": zip_path})
    return csv_path


def _run_load(*, mongo, csv_path: str, states: list[str], load_id: str) -> dict:
    """Execute one full load cycle: drain -> partition -> metadata -> workers -> indexes.
    Returns the drain result for assertion access."""
    cfg = _base_config(states=states, load_id=load_id)
    cfg["csv_path"] = csv_path

    drain_result = drain_staging_fn(cfg)

    partitions = partition_file_fn(cfg)
    metadata_ids = write_metadata_fn({**cfg, "partitions": partitions})

    for i, partition in enumerate(partitions):
        worker_cfg = {**cfg, "metadata_id": metadata_ids[i], **partition}
        provider_worker_fn(worker_cfg)

    ensure_postload_indexes_fn(cfg)
    return drain_result


# ── Step A: load both states into a fresh test collection ──────────────────
def test_a_initial_two_state_load(mongo, csv_path):
    """REQ-T-003 setup: load STATE_KEEP + STATE_DRAIN into a brand-new test collection."""
    drain = _run_load(
        mongo=mongo,
        csv_path=csv_path,
        states=[STATE_KEEP, STATE_DRAIN],
        load_id=LOAD_ID_AB,
    )
    # First run on a state-scoped load: under new EPIC-010-F-006-S-001 semantics,
    # `states` is required and never empty, so drain always runs in state_scoped
    # mode. Collection didn't exist before this load, so deleted=0 is expected.
    assert drain["mode"] == "state_scoped", f"first-run drain mode: {drain}"
    assert drain["deleted"] == 0, f"first-run drain should delete nothing (collection didn't exist): {drain}"

    coll = mongo[TEST_DB][TEST_PROVIDERS_COLL]
    keep_count = coll.count_documents({"practice_address.state": STATE_KEEP})
    drain_count = coll.count_documents({"practice_address.state": STATE_DRAIN})
    assert keep_count > 0, f"Step A: expected {STATE_KEEP} records loaded, got {keep_count}"
    assert drain_count > 0, f"Step A: expected {STATE_DRAIN} records loaded, got {drain_count}"
    print(f"\n[Step A] {STATE_KEEP}={keep_count}, {STATE_DRAIN}={drain_count}")


# ── Step B: capture survivor count ─────────────────────────────────────────
@pytest.fixture(scope="module")
def survivor_count_before(mongo):
    """Capture STATE_KEEP record count after Step A. Used by Step D assertion."""
    coll = mongo[TEST_DB][TEST_PROVIDERS_COLL]
    n = coll.count_documents({"practice_address.state": STATE_KEEP})
    assert n > 0, f"Step B: no {STATE_KEEP} records — Step A failed?"
    print(f"\n[Step B] survivor count captured: {STATE_KEEP}={n}")
    return n


# ── Step C: re-run the full load with only STATE_DRAIN in scope ────────────
def test_c_state_scoped_redrain(mongo, csv_path, survivor_count_before):
    """REQ-T-003 main exercise: re-run with states=[STATE_DRAIN] only.
    Drain must remove ONLY STATE_DRAIN records; STATE_KEEP records must persist."""
    drain = _run_load(
        mongo=mongo,
        csv_path=csv_path,
        states=[STATE_DRAIN],
        load_id=LOAD_ID_C,
    )
    assert drain["mode"] == "state_scoped", (
        f"Step C: drain mode must be state_scoped (REQ-T-003), got {drain}"
    )
    assert drain["states"] == [STATE_DRAIN], (
        f"Step C: drain states must equal [{STATE_DRAIN}], got {drain['states']}"
    )
    assert drain["deleted"] > 0, (
        f"Step C: drain must have deleted some {STATE_DRAIN} records, deleted={drain['deleted']}"
    )
    print(f"\n[Step C] state-scoped drain deleted {drain['deleted']} {STATE_DRAIN} records")


# ── Step D: assert survivor preserved ──────────────────────────────────────
def test_d_survivor_count_unchanged(mongo, survivor_count_before):
    """REQ-T-003 assertion: STATE_KEEP record count must be identical to Step B capture.
    If this fails, the drain incorrectly deleted out-of-scope records."""
    coll = mongo[TEST_DB][TEST_PROVIDERS_COLL]
    survivor_count_after = coll.count_documents({"practice_address.state": STATE_KEEP})
    print(f"\n[Step D] survivor count after: {STATE_KEEP}={survivor_count_after} (was {survivor_count_before})")
    assert survivor_count_after == survivor_count_before, (
        f"REQ-T-003 VIOLATED: {STATE_KEEP} record count changed during a "
        f"{STATE_DRAIN}-scoped drain. Before: {survivor_count_before}, After: {survivor_count_after}. "
        f"State-scoped drain must preserve out-of-scope records."
    )


# ── Step E: confirm distinct load timestamps via DataLoadMetadata ──────────
def test_e_distinct_load_timestamps(mongo):
    """Confirm the two loads (AB and C) registered as distinct events with
    different timestamps in DataLoadMetadata. Per Skip 2026-05-05: provider
    docs carry load_id but no direct timestamp — the indirect proof goes
    through metadata.date keyed by load_id.
    """
    md = mongo[TEST_DB][TEST_METADATA_COLL]
    rec_ab = md.find_one({"load_id": LOAD_ID_AB}, sort=[("date", 1)])
    rec_c  = md.find_one({"load_id": LOAD_ID_C},  sort=[("date", 1)])
    assert rec_ab is not None, f"Step E: no metadata record for load_id={LOAD_ID_AB}"
    assert rec_c  is not None, f"Step E: no metadata record for load_id={LOAD_ID_C}"
    assert rec_ab["date"] != rec_c["date"], (
        f"Step E: AB and C must have different timestamps. "
        f"AB.date={rec_ab['date']}, C.date={rec_c['date']}"
    )
    assert rec_ab["date"] < rec_c["date"], (
        f"Step E: AB must precede C in time. AB.date={rec_ab['date']}, C.date={rec_c['date']}"
    )
    print(f"\n[Step E] AB.date={rec_ab['date']}  C.date={rec_c['date']}  (distinct, AB<C)")


# Step F (cleanup) intentionally omitted per Skip 2026-05-05.
# Test collections persist for inspection:
#   test_DataPipelines.test_providers_<uuid>
#   test_DataPipelines.test_DataLoadMetadata_<uuid>
