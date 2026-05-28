# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""recover_county_activity - the post-load county-recovery worker.

Two activity entry points wired into the provider_pipeline_orchestrator
recovery phase:

  build_recovery_assignments_activity
    INGEST. Aggregates the cohort of NPIs that carry at least one
    practice address currently tagged geocoder_failed. Uses
    addresses.county.source_1 (created by ensure_provider_indexes).
    Splits the NPI list into batch_size-sized chunks and returns a
    chunk_index that the orchestrator feeds straight into the
    work_manager entity under work_kind="repair_chunks".

  process_recovery_assignment_activity
    WORKER. Takes one assignment ({npis: [...]}), fetches each provider
    doc by NPI from Mongo, re-runs the pass chain (pass2 census ->
    pass3 billing -> pass4 maps -> pass6 nppes) against the failed
    addresses, then writes the new addresses array back per NPI in one
    bulk_write. Per-API rate limiting is shared with the load phase via
    the _TokenBucket instances in process_assignment_activity (same
    process, same buckets).

Per Skip 2026-05-28:
  - Per-NPI assignments (real records), not deduplicated address keys.
  - Cache starts empty for the recovery phase. The shared cache from the
    load phase remains in-process and gives us a cheap free win when an
    address repeats across the cohort, but we don't pre-seed or warm it.
  - Same ingest -> process -> write-back pattern the CMS load uses.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from pymongo import MongoClient, UpdateOne


def _get_client() -> MongoClient:
    return MongoClient(
        os.environ["MONGO_connectionString"],
        serverSelectionTimeoutMS=120_000,
        maxPoolSize=64,
    )


def _split_collection(fqn: str | None) -> tuple[str, str]:
    if not fqn:
        fqn = f"{os.environ.get('ENV_PREFIX', 'dev')}_PublicHealthData.providers"
    db, coll = fqn.split(".", 1)
    return db, coll


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_recovery_assignments_fn(config: dict) -> dict:
    """INGEST: list NPIs needing county recovery, split into assignments.

    Returns:
      {
        "total_npis": int,
        "chunk_index": [[npi, npi, ...], [npi, ...], ...],
      }

    chunk_index is the work_manager seed format for work_kind="repair_chunks":
    each entry is the NPI list for one assignment. Empty cohort -> empty
    chunk_index -> work_manager seed sets total_chunks=0 -> recovery
    workers exit on first next_assignment.
    """
    from process_assignment_activity import _RECOVERY_COHORT_SOURCES

    db_name, coll_name = _split_collection(config.get("provider_collection"))
    batch_size = int(config.get("recovery_batch_size", 50))

    client = _get_client()
    coll = client[db_name][coll_name]

    # Distinct NPIs touched by at least one non-terminal failure label
    # (pass2/3/4_failed). Pass6_failed records are chain-exhausted and
    # excluded — recovery has nowhere left in the funnel to resume them.
    # The addresses.county.source_1 multikey index covers the $in match.
    cursor = coll.find(
        {"addresses.county.source": {"$in": list(_RECOVERY_COHORT_SOURCES)}},
        {"npi": 1, "_id": 0},
    )
    npis: list[str] = []
    for row in cursor:
        npi = row.get("npi")
        if npi:
            npis.append(str(npi))

    chunk_index: list[list[str]] = []
    for i in range(0, len(npis), batch_size):
        chunk_index.append(npis[i:i + batch_size])

    logging.info(
        "build_recovery_assignments: %d npis -> %d chunks (batch_size=%d)",
        len(npis), len(chunk_index), batch_size,
    )
    return {
        "total_npis": len(npis),
        "chunk_index": chunk_index,
        "batch_size": batch_size,
    }


def process_recovery_assignment_fn(config: dict) -> dict:
    """WORKER: re-run the pass chain on the assignment's NPIs.

    Reuses process_assignment_activity's pass2/3/4/6 implementations and
    its _TokenBucket instances (_CENSUS, _GMAPS, _NPPES) so this activity
    shares the same per-API rate ceiling with the load phase running in
    the same process. The shared _ADDR_CACHE there is also used here for
    free; per Skip we start it warm-or-empty depending on whether the load
    phase populated it during this run.
    """
    t0 = time.time()
    load_id = config["load_id"]
    worker_id = config.get("worker_id")
    assignment = config.get("assignment") or {}
    chunk_id = assignment.get("chunk_id")
    npis = assignment.get("npis") or []
    n_in = len(npis)

    if not npis:
        return _empty_result(chunk_id, n_in, t0)

    db_name, coll_name = _split_collection(config.get("provider_collection"))
    client = _get_client()
    coll = client[db_name][coll_name]

    from process_assignment_activity import (
        _pass2_census, _pass3_billing, _pass4_maps, _pass6_nppes,
    )

    # Fetch the assignment's docs in one round-trip — we need the full
    # addresses array, so no projection narrowing.
    docs = {}
    for d in coll.find({"npi": {"$in": npis}}):
        npi = d.get("npi")
        if npi:
            docs[str(npi)] = d

    ops: list[UpdateOne] = []
    n_resolved_addresses = 0
    n_still_failed_addresses = 0

    for npi in npis:
        doc = docs.get(str(npi))
        if doc is None:
            continue
        # Snapshot the failed-practice-address count for metrics.
        pre_failed = _count_failed_practice(doc)
        try:
            _pass2_census(doc)
            _pass3_billing(doc)
            _pass4_maps(doc)
            _pass6_nppes(doc)
        except Exception as exc:
            logging.warning(
                "recovery: npi=%s pass-chain failed: %s", npi, exc,
            )
            continue
        post_failed = _count_failed_practice(doc)
        n_resolved_addresses += max(0, pre_failed - post_failed)
        n_still_failed_addresses += post_failed

        ops.append(UpdateOne(
            {"npi": str(npi)},
            {"$set": {
                "addresses": doc.get("addresses") or [],
                "recovered_at": _iso_now(),
                "recovery_load_id": load_id,
            }},
        ))

    n_modified = 0
    if ops:
        try:
            r = coll.bulk_write(ops, ordered=False)
            n_modified = int(r.modified_count or 0)
        except Exception as exc:
            logging.error("recovery: bulk_write failed: %s", exc)
            raise

    elapsed = time.time() - t0
    metric = {
        "load_id": load_id,
        "emitted_at": _iso_now(),
        "kind": "county_recovery_chunk",
        "worker_id": worker_id,
        "chunk_id": chunk_id,
        "npis_in_chunk": n_in,
        "providers_found": len(docs),
        "providers_modified": n_modified,
        "addresses_resolved": n_resolved_addresses,
        "addresses_still_failed": n_still_failed_addresses,
        "duration_seconds": round(elapsed, 2),
    }
    _emit_chunk_metric(client, metric)
    logging.info(
        "recovery chunk %s: npis=%d found=%d modified=%d resolved=%d still_failed=%d in %.1fs",
        chunk_id, n_in, len(docs), n_modified,
        n_resolved_addresses, n_still_failed_addresses, elapsed,
    )
    return {
        "chunk_id": chunk_id,
        "npis_in_chunk": n_in,
        "providers_modified": n_modified,
        "addresses_resolved": n_resolved_addresses,
        "addresses_still_failed": n_still_failed_addresses,
        "duration_seconds": elapsed,
    }


def _count_failed_practice(doc: dict) -> int:
    from process_assignment_activity import _RECOVERY_COHORT_SOURCES, _FAILED_PASS6
    n = 0
    for a in doc.get("addresses") or []:
        if not isinstance(a, dict):
            continue
        if a.get("address_type") != "practice":
            continue
        c = a.get("county") or {}
        src = c.get("source")
        if src in _RECOVERY_COHORT_SOURCES or src == _FAILED_PASS6:
            n += 1
    return n


def _empty_result(chunk_id, n_in: int, t0: float) -> dict:
    return {
        "chunk_id": chunk_id,
        "npis_in_chunk": n_in,
        "providers_modified": 0,
        "addresses_resolved": 0,
        "addresses_still_failed": 0,
        "duration_seconds": round(time.time() - t0, 2),
    }


def _emit_chunk_metric(client: MongoClient, doc: dict) -> None:
    db_name = os.environ.get("ENV_PREFIX", "dev") + "_PublicHealthData"
    try:
        client[db_name]["pipeline_run_metrics"].insert_one(doc)
    except Exception as exc:
        logging.warning("recovery metric emit failed: %s", exc)
