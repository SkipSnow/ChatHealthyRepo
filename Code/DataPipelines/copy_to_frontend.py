# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""copy_to_frontend.py — copies runtime-read collections from ChatHealthyDataPipelines
to ChatHealthyFrontEnd so the chat app only needs one connection string.

Collections copied:
  PublicHealthData.SpecialtyMetaData  (pipeline → frontend)

Triggered via POST /api/Router with ChatHealthyTask: CopyToFrontEnd.
"""

import logging
import os
import time

from pymongo import MongoClient

BATCH_SIZE = 10_000

# Always-copied collections: (src_coll, dst_coll)
# Source and destination DB names are built from env_prefix at runtime.
_STATIC_COLLECTIONS = [
    ("SpecialtyMetaData", "SpecialtyMetaData"),
]


def _copy_collection(src_db, dst_db, src_coll: str, dst_coll: str, query: dict = None) -> dict:
    src = src_db[src_coll]
    dst = dst_db[dst_coll]
    label = src_coll if src_coll == dst_coll else f"{src_coll}→{dst_coll}"
    q = query or {}

    total = src.count_documents(q)
    existing = dst.count_documents({})

    if existing >= total and total > 0 and not q:
        logging.info("%s: already has %s docs — skipping", label, f"{existing:,}")
        return {"collection": label, "copied": 0, "skipped": True}

    if existing > 0:
        logging.info("%s: dropping destination (%s docs) before copy", label, f"{existing:,}")
        dst.drop()

    logging.info("%s: copying %s docs (filter: %s)", label, f"{total:,}", q or "none")
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
                logging.info("%s: %s / %s  (%.0f doc/s)", label, f"{copied:,}", f"{total:,}", rate)
                batch = []
        if batch:
            dst.insert_many(batch, ordered=False)
            copied += len(batch)
    finally:
        cursor.close()

    elapsed = time.time() - start
    logging.info("%s: done — %s docs in %.1f s", label, f"{copied:,}", elapsed)
    return {"collection": label, "copied": copied}


def snapshot_collection_fn(config: dict) -> dict:
    """Server-side copy of a PublicHealthData collection using aggregate $out.

    No data moves over the wire — MongoDB copies internally.
    Replaces destination if it already exists.
    """
    source = config.get("source", "providers")
    destination = config.get("destination")
    if not destination:
        raise ValueError("snapshot: 'destination' collection name is required in payload")

    conn = os.environ.get("MONGO_connectionString")
    if not conn:
        raise ValueError("MONGO_connectionString not set")

    env_prefix = config.get("env_prefix", "dev")
    db_name = f"{env_prefix}_PublicHealthData" if env_prefix else "PublicHealthData"
    client = MongoClient(conn, serverSelectionTimeoutMS=30_000)
    try:
        db = client[db_name]
        count_before = db[source].count_documents({})
        logging.info("Snapshot: %s (%s docs) → %s", source, f"{count_before:,}", destination)
        list(db[source].aggregate([{"$out": destination}], allowDiskUse=True))
        count_after = db[destination].count_documents({})
        logging.info("Snapshot complete: %s → %s (%s docs)", source, destination, f"{count_after:,}")
        return {"source": source, "destination": destination, "source_count": count_before, "copied": count_after}
    finally:
        client.close()


def _create_frontend_vector_index(frontend_client: MongoClient, db_name: str, coll_name: str = "providers") -> dict:
    """Create Atlas Vector Search index on the FrontEnd cluster's providers collection.

    Includes a filter field on practice_address.state so $vectorSearch can pre-filter.
    Idempotent — silently skips if the index already exists.
    """
    collection = frontend_client[db_name][coll_name]
    index_name = "provider_vector_index"
    try:
        collection.create_search_index({
            "name": index_name,
            "type": "vectorSearch",
            "definition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": 3072,
                        "similarity": "cosine",
                    },
                    {
                        "type": "filter",
                        "path": "practice_address.state",
                    },
                ]
            },
        })
        logging.info("Frontend vector index '%s' created on %s.%s", index_name, db_name, coll_name)
    except Exception as exc:
        if "already exists" in str(exc).lower() or "duplicate" in str(exc).lower():
            logging.info("Frontend vector index '%s' already exists — skipping.", index_name)
        else:
            raise
    return {"index": index_name, "db": db_name, "collection": coll_name, "status": "ready"}


def create_frontend_vector_index_fn(config: dict) -> dict:
    """Standalone task: create (or verify) the vector search index on the FrontEnd cluster.

    config:
      env_prefix   — database prefix, e.g. "dev" → dev_PublicHealthData (default: "dev")
      collection   — collection name (default: "providers")
    """
    frontend_conn = os.environ.get("MONGO_FRONTEND_connectionString")
    if not frontend_conn:
        raise ValueError("MONGO_FRONTEND_connectionString not set")
    env_prefix = config.get("env_prefix", "dev")
    db_name    = f"{env_prefix}_PublicHealthData" if env_prefix else "PublicHealthData"
    coll_name  = config.get("collection", "providers")
    client = MongoClient(frontend_conn, serverSelectionTimeoutMS=30_000)
    try:
        return _create_frontend_vector_index(client, db_name, coll_name)
    finally:
        client.close()


def run_copy_to_frontend(config: dict) -> dict:
    from cluster_lifecycle_manager import ClusterLifecycleManager, ResourceReservation

    pipeline_conn  = os.environ.get("MONGO_connectionString")
    frontend_conn  = os.environ.get("MONGO_FRONTEND_connectionString")

    if not pipeline_conn:
        raise ValueError("MONGO_connectionString not set")
    if not frontend_conn:
        raise ValueError("MONGO_FRONTEND_connectionString not set")

    # Register cluster reservation — wakes pipeline DB if paused
    env_prefix = config.get("env_prefix", "dev")
    manager = ClusterLifecycleManager(
        get_db_fn=lambda: MongoClient(frontend_conn),
        env_prefix=env_prefix,
    )
    reservation = ResourceReservation(
        job_id=f"copy_to_frontend_{int(time.time())}",
        requester="CopyToFrontEnd",
        cluster_name=config.get("pipeline_cluster", "ChatHealthyDataPipelines"),
        expected_duration_minutes=config.get("expected_duration_minutes", 60),
    )
    manager.register(reservation)
    logging.info("Reservation registered: %s", reservation.job_id)

    # env_prefix scopes the destination DB on the FrontEnd cluster.
    # "dev" → dev_PublicHealthData; "" or omitted → PublicHealthData (legacy)
    env_prefix   = config.get("env_prefix", "dev")
    dst_db_name  = f"{env_prefix}_PublicHealthData" if env_prefix else "PublicHealthData"

    # Build provider filter from states config.
    # states may be a list ["DE", "MS"] or a dict {"mode": "include", "list": [...]}
    states = config.get("states")
    if isinstance(states, dict):
        state_list = states.get("list", [])
        mode = states.get("mode", "include")
    elif isinstance(states, list):
        state_list = states
        mode = "include"
    else:
        state_list = []
        mode = "include"

    if state_list and mode == "include":
        provider_query = {"practice_address.state": {"$in": state_list}}
    elif state_list and mode == "exclude":
        provider_query = {"practice_address.state": {"$nin": state_list}}
    else:
        provider_query = None  # no providers copied unless states specified

    pipeline_client = MongoClient(pipeline_conn,  serverSelectionTimeoutMS=30_000)
    frontend_client = MongoClient(frontend_conn,  serverSelectionTimeoutMS=30_000)

    results = []
    try:
        src_db_name = config.get("src_db", "dev_PublicHealthData")

        for src_coll, dst_coll in _STATIC_COLLECTIONS:
            logging.info("=== %s.%s → frontend:%s.%s ===", src_db_name, src_coll, dst_db_name, dst_coll)
            results.append(_copy_collection(
                pipeline_client[src_db_name], frontend_client[dst_db_name], src_coll, dst_coll
            ))

        if provider_query is not None:
            logging.info(
                "=== %s.providers → frontend:%s.providers (states: %s) ===",
                src_db_name, dst_db_name, state_list,
            )
            results.append(_copy_collection(
                pipeline_client[src_db_name],
                frontend_client[dst_db_name],
                "providers",
                "providers",
                query=provider_query,
            ))

            # Create vector search index on FrontEnd after provider copy
            logging.info("Creating vector search index on frontend:%s.providers", dst_db_name)
            idx_result = _create_frontend_vector_index(frontend_client, dst_db_name)
            results.append(idx_result)

    finally:
        pipeline_client.close()
        frontend_client.close()
        # Always release reservation — cluster shuts down if last one
        manager.release(reservation)
        logging.info("Reservation released: %s", reservation.job_id)

    logging.info("CopyToFrontEnd complete: %s", results)
    return {"status": "complete", "collections": results}
