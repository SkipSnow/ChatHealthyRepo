# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Opus 4.6 (Anthropic).
#
# promote_data_fn.py — Promote data between environments on the frontend cluster.
#
# Copies collections from {from_env}_PublicHealthData to {to_env}_PublicHealthData
# on the SAME cluster (MONGO_FRONTEND_connectionString). No pipeline cluster needed.
#
# Triggered via POST /api/Router with ChatHealthyTask: PromoteData.
#
# Payload:
#   from_env:     source environment (e.g., "dev")
#   to_env:       target environment (e.g., "qa", "prod")
#   collections:  list of collection names (e.g., ["SpecialtyMetaData", "providers"])
#   states:       optional state filter for providers (e.g., ["DE"])

import logging
import os
import time

from pymongo import MongoClient

BATCH_SIZE = 5_000

log = logging.getLogger("promote_data")


def _copy_collection(client, from_db_name: str, to_db_name: str,
                     coll_name: str, query: dict = None) -> dict:
    """Copy a single collection within the same cluster."""
    src = client[from_db_name][coll_name]
    dst = client[to_db_name][coll_name]
    q = query or {}

    total = src.count_documents(q)
    existing = dst.count_documents({})

    log.info("%s.%s (%s docs) -> %s.%s (%s docs)",
             from_db_name, coll_name, f"{total:,}",
             to_db_name, coll_name, f"{existing:,}")

    if total == 0:
        log.info("  Source is empty — skipping")
        return {"collection": coll_name, "copied": 0, "skipped": True, "reason": "source_empty"}

    if existing > 0:
        log.info("  Dropping destination (%s docs)", f"{existing:,}")
        dst.drop()

    log.info("  Copying %s docs...", f"{total:,}")
    cursor = src.find(q, batch_size=BATCH_SIZE, no_cursor_timeout=True)
    batch = []
    copied = 0
    start = time.time()

    try:
        for doc in cursor:
            batch.append(doc)
            if len(batch) >= BATCH_SIZE:
                dst.insert_many(batch, ordered=False)
                copied += len(batch)
                elapsed = time.time() - start
                rate = copied / elapsed if elapsed > 0 else 0
                log.info("  %s / %s (%.0f doc/s)", f"{copied:,}", f"{total:,}", rate)
                batch = []
        if batch:
            dst.insert_many(batch, ordered=False)
            copied += len(batch)
    finally:
        cursor.close()

    elapsed = time.time() - start
    log.info("  Done: %s docs in %.1f s", f"{copied:,}", elapsed)
    return {"collection": coll_name, "copied": copied, "elapsed_s": round(elapsed, 1)}


def run_promote_data(config: dict) -> dict:
    """Promote data between environments on the frontend cluster.

    config:
        from_env:     source environment prefix (required, e.g., "dev")
        to_env:       target environment prefix (required, e.g., "qa" or "prod")
        collections:  list of collection names to copy (required)
        states:       optional list of state codes to filter providers (e.g., ["DE"])
    """
    frontend_conn = os.environ.get("MONGO_FRONTEND_connectionString")
    if not frontend_conn:
        raise ValueError("MONGO_FRONTEND_connectionString not set")

    from_env = config.get("from_env")
    to_env = config.get("to_env")
    collections = config.get("collections", [])
    states = config.get("states", [])

    if not from_env:
        raise ValueError("from_env is required")
    if not to_env:
        raise ValueError("to_env is required")
    if not collections:
        raise ValueError("collections list is required (e.g., ['SpecialtyMetaData', 'providers'])")
    if from_env == to_env:
        raise ValueError(f"from_env and to_env cannot be the same: {from_env}")

    from_db = f"{from_env}_PublicHealthData"
    to_db = f"{to_env}_PublicHealthData"

    log.info("=== PROMOTE DATA: %s -> %s ===", from_db, to_db)
    log.info("Collections: %s", collections)
    if states:
        log.info("State filter: %s", states)

    client = MongoClient(frontend_conn, serverSelectionTimeoutMS=30_000)
    results = []

    try:
        for coll_name in collections:
            # Build query — state filter only applies to providers
            query = None
            if coll_name == "providers" and states:
                query = {"practice_address.state": {"$in": states}}

            result = _copy_collection(client, from_db, to_db, coll_name, query)
            results.append(result)

    finally:
        client.close()

    total_copied = sum(r.get("copied", 0) for r in results)
    log.info("=== PROMOTE COMPLETE: %s -> %s (%s docs total) ===",
             from_db, to_db, f"{total_copied:,}")

    return {
        "status": "complete",
        "from": from_db,
        "to": to_db,
        "collections": results,
        "total_copied": total_copied,
    }
