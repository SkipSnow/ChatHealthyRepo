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


# Hard-coded recovery cohort (the three resume-able failure values our
# code writes; pass6_failed is terminal and excluded). The strings are
# the same ones process_assignment_activity writes; the schema enum
# documents them. We do not load the enum from anywhere — the values
# are closed-loop inside our own code.
_RECOVERY_FAILURE_VALUES = (
    "geocoder_pass2_failed",
    "geocoder_pass3_failed",
    "geocoder_pass4_failed",
)


def build_recovery_assignments_fn(config: dict) -> dict:
    """INGEST: query failed-source records, group by the failure value
    they carry, and emit stage-stamped assignments.

    Returns:
      {
        "total_npis": int,
        "per_stage": {stage: count, ...},
        "chunk_index": [
            {"stage": "geocoder_pass2_failed", "npis": [...]},
            {"stage": "geocoder_pass3_failed", "npis": [...]},
            {"stage": "geocoder_pass4_failed", "npis": [...]},
            ...
        ],
      }

    Each chunk is one assignment: a single stage value plus up to
    recovery_batch_size NPIs that carry that stage. Worker reads the
    stage off the assignment and dispatches to the right next pass.
    """
    db_name, coll_name = _split_collection(config.get("provider_collection"))
    batch_size = int(config.get("recovery_batch_size", 200))

    client = _get_client()
    coll = client[db_name][coll_name]

    # Per-stage NPI buckets keyed by the failure value the records carry.
    # A provider is placed in the bucket of the failure value it carries
    # on any of its addresses; if it carries more than one we take the
    # earliest in the funnel (pass2 < pass3 < pass4) so the resume work
    # is done at the right step. The aggregation groups by NPI so the
    # cohort has at most one row per NPI even if the providers collection
    # happens to carry duplicate documents for the same NPI — the
    # downstream worker assignments are NPI-disjoint by construction.
    funnel_order = {
        "geocoder_pass2_failed": 0,
        "geocoder_pass3_failed": 1,
        "geocoder_pass4_failed": 2,
    }
    per_stage: dict[str, list[str]] = {v: [] for v in _RECOVERY_FAILURE_VALUES}
    pipeline = [
        {"$match": {"addresses.county.source": {"$in": list(_RECOVERY_FAILURE_VALUES)}}},
        {"$unwind": "$addresses"},
        {"$match": {"addresses.county.source": {"$in": list(_RECOVERY_FAILURE_VALUES)}}},
        {"$group": {
            "_id": "$npi",
            "sources": {"$addToSet": "$addresses.county.source"},
        }},
    ]
    for row in coll.aggregate(pipeline, allowDiskUse=True):
        npi = row.get("_id")
        if not npi:
            continue
        earliest: str | None = None
        earliest_rank: int = 99
        for src in row.get("sources") or []:
            r = funnel_order.get(src, 99)
            if r < earliest_rank:
                earliest_rank = r
                earliest = src
        if earliest is not None:
            per_stage[earliest].append(str(npi))

    chunk_index: list[dict] = []
    for stage, npis in per_stage.items():
        for i in range(0, len(npis), batch_size):
            chunk_index.append({
                "stage": stage,
                "npis": npis[i:i + batch_size],
            })

    total_npis = sum(len(v) for v in per_stage.values())
    logging.info(
        "build_recovery_assignments: %d npis -> %d chunks (batch_size=%d) per_stage=%s",
        total_npis, len(chunk_index), batch_size,
        {k: len(v) for k, v in per_stage.items()},
    )
    return {
        "total_npis": total_npis,
        "per_stage": {k: len(v) for k, v in per_stage.items()},
        "chunk_index": chunk_index,
        "batch_size": batch_size,
    }


def process_recovery_assignment_fn(config: dict) -> dict:
    """WORKER: re-run the existing pass chain on the assignment's NPIs.

    The assignment carries a single stage value as a tag for metrics.
    The worker reuses the existing chain in process_assignment_activity
    (_pass2_census -> _pass4_maps -> _pass6_nppes); each pass's selector
    decides per-address whether to act, so the "continue down the funnel
    on lack of result" semantics fall out of the existing logic. No new
    dispatch code, no new stage-routing branch.

    Pass 3 (Census geocode of the business address) was removed; records
    on disk that still carry `geocoder_pass3_failed` on a practice
    address are intentionally stranded — no recovery stage targets them.
    """
    t0 = time.time()
    load_id = config["load_id"]
    worker_id = config.get("worker_id")
    assignment = config.get("assignment") or {}
    chunk_id = assignment.get("chunk_id")
    stage = assignment.get("stage")
    npis = assignment.get("npis") or []
    n_in = len(npis)

    if not npis or not stage:
        return _empty_result(chunk_id, stage, n_in, t0)

    db_name, coll_name = _split_collection(config.get("provider_collection"))
    client = _get_client()
    coll = client[db_name][coll_name]

    from process_assignment_activity import (
        _pass2_census, _pass4_maps, _pass6_nppes,
        _stamp_urban, _load_rucc, _embed_batch,
    )

    rucc = _load_rucc(
        config.get("env_prefix", os.environ.get("ENV_PREFIX", "dev")),
        config.get("blob_container", "provider-data"),
    )

    # Fetch the assignment's docs in one round-trip.
    docs = {}
    for d in coll.find({"npi": {"$in": npis}}):
        npi = d.get("npi")
        if npi:
            docs[str(npi)] = d

    resolved_docs: list[dict] = []
    n_addresses_at_stage_before = 0
    n_addresses_at_stage_after = 0

    for npi in npis:
        doc = docs.get(str(npi))
        if doc is None:
            continue
        before = _count_addresses_at_stage(doc, stage)
        # Snapshot per-address county.source BEFORE re-running the pass
        # chain. After the chain runs, addresses whose source changed (i.e.
        # the recovery actually resolved them) get a "_recovered" suffix
        # on their source label so the recovery phase is distinguishable
        # from a first-pass success at terminal — per Skip 2026-05-29.
        sources_before = [
            (addr.get("county") or {}).get("source")
            if isinstance(addr, dict) else None
            for addr in (doc.get("addresses") or [])
        ]
        try:
            _pass2_census(doc)
            _pass4_maps(doc)
            _pass6_nppes(doc)
            _stamp_urban(doc, rucc)
        except Exception as exc:
            logging.warning(
                "recovery: npi=%s stage=%s chain raised: %s",
                npi, stage, exc,
            )
            continue
        # Label newly-resolved addresses with the recovery suffix. Skip
        # addresses that didn't change source (already-resolved or still-
        # failing) and addresses whose new source is itself a failure
        # label (no point suffixing a failure).
        for i, addr in enumerate(doc.get("addresses") or []):
            if not isinstance(addr, dict):
                continue
            county = addr.get("county")
            if not isinstance(county, dict):
                continue
            src = county.get("source")
            if (
                src
                and src != sources_before[i]
                and not src.endswith("_failed")
                and not src.endswith("_recovered")
            ):
                county["source"] = f"{src}_recovered"
        after = _count_addresses_at_stage(doc, stage)
        n_addresses_at_stage_before += before
        n_addresses_at_stage_after += after
        resolved_docs.append(doc)

    # Embed every recovered doc in one batched OpenAI call (should_embed
    # filters out any that still carry a recoverable failure label).
    _embed_batch(resolved_docs)

    ops: list[UpdateOne] = []
    for doc in resolved_docs:
        set_fields = {
            "addresses": doc.get("addresses") or [],
            "recovered_at": _iso_now(),
            "recovery_load_id": load_id,
            "recovery_stage": stage,
        }
        if doc.get("embedding") is not None:
            set_fields["embedding"] = doc["embedding"]
            set_fields["embedding_model"] = doc.get("embedding_model")
            set_fields["embedding_version"] = doc.get("embedding_version")
        ops.append(UpdateOne({"npi": str(doc["npi"])}, {"$set": set_fields}))

    n_modified = 0
    if ops:
        try:
            r = coll.bulk_write(ops, ordered=False)
            n_modified = int(r.modified_count or 0)
        except Exception as exc:
            logging.error("recovery: bulk_write failed: %s", exc)
            raise

    elapsed = time.time() - t0
    n_moved_forward = max(0, n_addresses_at_stage_before - n_addresses_at_stage_after)
    metric = {
        "load_id": load_id,
        "emitted_at": _iso_now(),
        "kind": "county_recovery_chunk",
        "worker_id": worker_id,
        "chunk_id": chunk_id,
        "stage": stage,
        "npis_in_chunk": n_in,
        "providers_found": len(docs),
        "providers_modified": n_modified,
        "addresses_at_stage_before": n_addresses_at_stage_before,
        "addresses_at_stage_after": n_addresses_at_stage_after,
        "addresses_moved_forward": n_moved_forward,
        "duration_seconds": round(elapsed, 2),
    }
    _emit_chunk_metric(client, metric)
    logging.info(
        "recovery chunk %s stage=%s npis=%d modified=%d moved_forward=%d in %.1fs",
        chunk_id, stage, n_in, n_modified, n_moved_forward, elapsed,
    )
    return {
        "chunk_id": chunk_id,
        "stage": stage,
        "npis_in_chunk": n_in,
        "providers_modified": n_modified,
        "addresses_moved_forward": n_moved_forward,
        "duration_seconds": elapsed,
    }


def _count_addresses_at_stage(doc: dict, stage: str) -> int:
    n = 0
    for a in doc.get("addresses") or []:
        if not isinstance(a, dict):
            continue
        c = a.get("county") or {}
        if c.get("source") == stage:
            n += 1
    return n


def _empty_result(chunk_id, stage, n_in: int, t0: float) -> dict:
    return {
        "chunk_id": chunk_id,
        "stage": stage,
        "npis_in_chunk": n_in,
        "providers_modified": 0,
        "addresses_moved_forward": 0,
        "duration_seconds": round(time.time() - t0, 2),
    }


def _emit_chunk_metric(client: MongoClient, doc: dict) -> None:
    db_name = os.environ.get("ENV_PREFIX", "dev") + "_PublicHealthData"
    try:
        client[db_name]["pipeline_run_metrics"].insert_one(doc)
    except Exception as exc:
        logging.warning("recovery metric emit failed: %s", exc)
