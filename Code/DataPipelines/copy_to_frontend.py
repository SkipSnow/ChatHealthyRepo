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
    ("provider_quality", "provider_quality"),
    ("ICD10Codes", "ICD10Codes"),
    ("ZipCountyCrosswalk", "ZipCountyCrosswalk"),
    ("drug_crosswalk_cache", "drug_crosswalk_cache"),
]


def _copy_collection(src_db, dst_db, src_coll: str, dst_coll: str, query: dict = None) -> dict:
    src = src_db[src_coll]
    dst = dst_db[dst_coll]
    label = src_coll if src_coll == dst_coll else f"{src_coll}→{dst_coll}"
    q = query or {}

    total = src.count_documents(q)
    existing = dst.estimated_document_count()  # instant, no collection scan

    if existing >= total and total > 0 and not q:
        logging.info("%s: already has %s docs — skipping", label, f"{existing:,}")
        return {"collection": label, "copied": 0, "skipped": True}

    if existing > 0 and not q:
        # Full collection copy — drop and replace
        logging.info("%s: dropping destination (%s docs) before full copy", label, f"{existing:,}")
        dst.drop()
    elif existing > 0 and q:
        # Filtered copy — delete only matching records, then append
        logging.info("%s: deleting filtered records before copy (query: %s)", label, q)
        dst.delete_many(q)

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
    # Parity verification — count destination and compare to source
    dst_count = dst.count_documents({})
    if dst_count != total:
        logging.error(
            "PARITY FAILURE %s: source=%s, destination=%s (expected %s)",
            label, f"{total:,}", f"{dst_count:,}", f"{total:,}",
        )
    else:
        logging.info("%s: PARITY OK — %s docs match", label, f"{dst_count:,}")
    logging.info("%s: done — %s docs in %.1f s", label, f"{copied:,}", elapsed)
    return {"collection": label, "copied": copied, "source_count": total, "destination_count": dst_count, "parity": dst_count == total}


def snapshot_collection_fn(config: dict) -> dict:
    """Server-side copy of a collection using aggregate $out.

    No data moves over the wire — MongoDB copies internally.
    Replaces destination if it already exists.

    config:
        source       — source collection name (default: "providers")
        destination  — destination collection name (required)
        env_prefix   — source database prefix (default: "dev")
        dst_db       — destination database name (optional, defaults to same as source)
    """
    source = config.get("source", "providers")
    destination = config.get("destination")
    if not destination:
        raise ValueError("snapshot: 'destination' collection name is required in payload")

    conn = os.environ.get("MONGO_connectionString")
    if not conn:
        raise ValueError("MONGO_connectionString not set")

    env_prefix = config.get("env_prefix", "dev")
    src_db_name = f"{env_prefix}_PublicHealthData" if env_prefix else "PublicHealthData"
    dst_db_name = config.get("dst_db", src_db_name)

    client = MongoClient(conn, serverSelectionTimeoutMS=30_000)
    try:
        db = client[src_db_name]
        count_before = db[source].count_documents({})
        logging.info("Snapshot: %s.%s (%s docs) → %s.%s",
                     src_db_name, source, f"{count_before:,}", dst_db_name, destination)

        if dst_db_name == src_db_name:
            out_stage = {"$out": destination}
        else:
            out_stage = {"$out": {"db": dst_db_name, "coll": destination}}

        list(db[source].aggregate([out_stage], allowDiskUse=True))
        count_after = client[dst_db_name][destination].count_documents({})
        logging.info("Snapshot complete: %s.%s → %s.%s (%s docs)",
                     src_db_name, source, dst_db_name, destination, f"{count_after:,}")
        return {"source": f"{src_db_name}.{source}", "destination": f"{dst_db_name}.{destination}",
                "source_count": count_before, "copied": count_after}
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
                    {
                        "type": "filter",
                        "path": "practice_address.city",
                    },
                    {
                        "type": "filter",
                        "path": "practice_address.zip",
                    },
                    {
                        "type": "filter",
                        "path": "county.name",
                    },
                    {
                        "type": "filter",
                        "path": "county.fips",
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


def _create_specialty_vector_index(frontend_client: MongoClient, db_name: str) -> dict:
    """Create Atlas Vector Search index on SpecialtyMetaData. Idempotent."""
    collection = frontend_client[db_name]["SpecialtyMetaData"]
    index_name = "specialty_vector_index"
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
                ]
            },
        })
        logging.info("Frontend vector index '%s' created on %s.SpecialtyMetaData", index_name, db_name)
    except Exception as exc:
        if "already exists" in str(exc).lower() or "duplicate" in str(exc).lower():
            logging.info("Frontend vector index '%s' already exists — skipping.", index_name)
        else:
            raise
    return {"index": index_name, "db": db_name, "collection": "SpecialtyMetaData", "status": "ready"}


def verify_frontend_indexes(frontend_client: MongoClient, db_name: str) -> dict:
    """DR-016: Verify all required vector search indexes exist. Fails promotion if missing.

    Required indexes:
      - provider_vector_index on providers
      - specialty_vector_index on SpecialtyMetaData
    """
    required = {
        "providers": "provider_vector_index",
        "SpecialtyMetaData": "specialty_vector_index",
    }
    results = {}
    missing = []
    for coll_name, idx_name in required.items():
        try:
            indexes = list(frontend_client[db_name][coll_name].list_search_indexes())
            found = any(idx.get("name") == idx_name for idx in indexes)
            results[idx_name] = "exists" if found else "MISSING"
            if not found:
                missing.append(f"{db_name}.{coll_name}/{idx_name}")
        except Exception as exc:
            results[idx_name] = f"ERROR: {exc}"
            missing.append(f"{db_name}.{coll_name}/{idx_name}")
    return {
        "db": db_name,
        "indexes": results,
        "missing": missing,
        "passed": len(missing) == 0,
    }


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


def migrate_environment(config: dict) -> dict:
    """Migrate all PublicHealthData collections from one environment to another.

    Same cluster, server-side $out. No data crosses the wire.

    config:
        src_env    — source environment prefix, e.g. "dev"
        dst_env    — destination environment prefix, e.g. "qa"
    """
    conn = os.environ.get("MONGO_connectionString")
    if not conn:
        raise ValueError("MONGO_connectionString not set")

    src_env = config.get("src_env", "dev")
    dst_env = config.get("dst_env", "qa")
    src_db_name = f"{src_env}_PublicHealthData"
    dst_db_name = f"{dst_env}_PublicHealthData"

    client = MongoClient(conn, serverSelectionTimeoutMS=30_000)
    try:
        src_db = client[src_db_name]
        collections = [c for c in src_db.list_collection_names() if not c.startswith("system.")]
        results = []

        for coll_name in sorted(collections):
            count_before = src_db[coll_name].count_documents({})
            logging.info("Migrate: %s.%s (%s docs) → %s.%s",
                         src_db_name, coll_name, f"{count_before:,}", dst_db_name, coll_name)
            list(src_db[coll_name].aggregate(
                [{"$out": {"db": dst_db_name, "coll": coll_name}}],
                allowDiskUse=True,
            ))
            count_after = client[dst_db_name][coll_name].count_documents({})
            results.append({
                "collection": coll_name,
                "source_count": count_before,
                "copied": count_after,
            })
            logging.info("Migrate: %s done — %s docs", coll_name, f"{count_after:,}")

        return {
            "status": "complete",
            "src": src_db_name,
            "dst": dst_db_name,
            "collections": results,
        }
    finally:
        client.close()


def migrate_small_collections(config: dict) -> dict:
    """Migrate non-provider collections via $out. Same cluster, fast."""
    conn = os.environ.get("MONGO_connectionString")
    if not conn:
        raise ValueError("MONGO_connectionString not set")
    src_env = config.get("src_env", "dev")
    dst_env = config.get("dst_env", "qa")
    src_db_name = f"{src_env}_PublicHealthData"
    dst_db_name = f"{dst_env}_PublicHealthData"

    client = MongoClient(conn, serverSelectionTimeoutMS=30_000)
    try:
        src_db = client[src_db_name]
        results = []
        for coll_name in sorted(src_db.list_collection_names()):
            if coll_name.startswith("system.") or coll_name == "providers":
                continue
            count = src_db[coll_name].count_documents({})
            logging.info("Migrate $out: %s.%s (%s docs) → %s.%s",
                         src_db_name, coll_name, f"{count:,}", dst_db_name, coll_name)
            list(src_db[coll_name].aggregate(
                [{"$out": {"db": dst_db_name, "coll": coll_name}}], allowDiskUse=True))
            results.append({"collection": coll_name, "copied": count})
        return {"collections": results}
    finally:
        client.close()


def migrate_chunk(config: dict) -> dict:
    """Copy a chunk of records by _id range within the same cluster (different databases)."""
    from bson import ObjectId
    conn = os.environ.get("MONGO_connectionString")
    if not conn:
        raise ValueError("MONGO_connectionString not set")

    src_env = config.get("src_env", "dev")
    dst_env = config.get("dst_env", "qa")
    src_db_name = f"{src_env}_PublicHealthData"
    dst_db_name = f"{dst_env}_PublicHealthData"
    coll_name = config.get("collection", "providers")
    job_number = config.get("job_number", 0)
    start_id = ObjectId(config["start_id"])
    end_id = ObjectId(config["end_id"])

    client = MongoClient(conn, serverSelectionTimeoutMS=30_000)
    try:
        src = client[src_db_name][coll_name]
        dst = client[dst_db_name][coll_name]
        query = {"_id": {"$gte": start_id, "$lte": end_id}}

        cursor = src.find(query, batch_size=BATCH_SIZE, no_cursor_timeout=True)
        batch = []
        copied = 0
        start_time = time.time()
        try:
            for doc in cursor:
                batch.append(doc)
                if len(batch) >= BATCH_SIZE:
                    dst.insert_many(batch, ordered=False)
                    copied += len(batch)
                    batch = []
            if batch:
                dst.insert_many(batch, ordered=False)
                copied += len(batch)
        finally:
            cursor.close()

        elapsed = time.time() - start_time
        logging.info("MigrateChunk %d: %s docs in %.1fs", job_number, f"{copied:,}", elapsed)
        return {"job_number": job_number, "copied": copied, "seconds": round(elapsed, 1)}
    finally:
        client.close()


def drop_destination(config: dict) -> dict:
    """Drop destination collection. Uses pipeline or frontend cluster based on config."""
    use_pipeline = config.get("use_pipeline_cluster", False)
    if use_pipeline:
        conn = os.environ.get("MONGO_connectionString")
        if not conn:
            raise ValueError("MONGO_connectionString not set")
    else:
        conn = os.environ.get("MONGO_FRONTEND_connectionString")
        if not conn:
            raise ValueError("MONGO_FRONTEND_connectionString not set")

    env_prefix = config.get("env_prefix", "dev")
    dst_db_name = f"{env_prefix}_PublicHealthData" if env_prefix else "PublicHealthData"
    coll_name = config.get("collection", "providers")

    client = MongoClient(conn, serverSelectionTimeoutMS=30_000)
    try:
        client[dst_db_name][coll_name].drop()
        cluster = "pipeline" if use_pipeline else "frontend"
        logging.info("Dropped %s.%s on %s cluster", dst_db_name, coll_name, cluster)
        return {"dropped": f"{dst_db_name}.{coll_name}", "cluster": cluster}
    finally:
        client.close()


def partition_source(config: dict) -> dict:
    """Count source records and compute _id boundaries for chunked copy.

    Uses $bucketAuto to sample boundary _ids without loading all docs into memory.
    O(1) memory regardless of collection size.

    config:
        env_prefix, src_db, collection, chunk_size, query
    """
    from bson import ObjectId

    pipeline_conn = os.environ.get("MONGO_connectionString")
    if not pipeline_conn:
        raise ValueError("MONGO_connectionString not set")

    env_prefix = config.get("env_prefix", "dev")
    src_db_name = config.get("src_db", f"{env_prefix}_PublicHealthData" if env_prefix else "PublicHealthData")
    coll_name = config.get("collection", "providers")
    chunk_size = config.get("chunk_size", 50_000)
    query = config.get("query", {})

    client = MongoClient(pipeline_conn, serverSelectionTimeoutMS=30_000)
    try:
        coll = client[src_db_name][coll_name]
        total = coll.count_documents(query)
        logging.info("Partition: %s.%s — %s docs, chunk_size=%s",
                     src_db_name, coll_name, f"{total:,}", f"{chunk_size:,}")

        if total == 0:
            return {"total": 0, "chunks": []}

        num_buckets = max(1, (total + chunk_size - 1) // chunk_size)

        # Use $bucketAuto to get boundary _ids — O(1) memory, no full scan
        pipeline = []
        if query:
            pipeline.append({"$match": query})
        pipeline.append({"$bucketAuto": {"groupBy": "$_id", "buckets": num_buckets}})

        buckets = list(coll.aggregate(pipeline, allowDiskUse=True))

        chunks = []
        for i, bucket in enumerate(buckets):
            chunks.append({
                "job_number": i,
                "start_id": str(bucket["_id"]["min"]),
                "end_id": str(bucket["_id"]["max"]),
                "count": bucket["count"],
                "is_last": i == len(buckets) - 1,
            })

        logging.info("Partition: %d chunks of ~%s", len(chunks), f"{chunk_size:,}")
        return {"total": total, "chunk_size": chunk_size, "chunks": chunks}
    finally:
        client.close()


def copy_chunk(config: dict) -> dict:
    """Copy a chunk of records by _id range from pipeline to frontend. Append mode.

    config:
        env_prefix, src_db, collection, job_number, start_id, end_id,
        drop_first (only job_number 0)
    """
    from bson import ObjectId

    pipeline_conn = os.environ.get("MONGO_connectionString")
    frontend_conn = os.environ.get("MONGO_FRONTEND_connectionString")
    if not pipeline_conn:
        raise ValueError("MONGO_connectionString not set")
    if not frontend_conn:
        raise ValueError("MONGO_FRONTEND_connectionString not set")

    env_prefix = config.get("env_prefix", "dev")
    src_db_name = config.get("src_db", f"{env_prefix}_PublicHealthData" if env_prefix else "PublicHealthData")
    dst_db_name = f"{env_prefix}_PublicHealthData" if env_prefix else "PublicHealthData"
    coll_name = config.get("collection", "providers")
    job_number = config.get("job_number", 0)
    start_id = ObjectId(config["start_id"])
    end_id = ObjectId(config["end_id"])

    pipeline_client = MongoClient(pipeline_conn, serverSelectionTimeoutMS=30_000)
    frontend_client = MongoClient(frontend_conn, serverSelectionTimeoutMS=30_000)

    try:
        src = pipeline_client[src_db_name][coll_name]
        dst = frontend_client[dst_db_name][coll_name]

        query = {"_id": {"$gte": start_id, "$lte": end_id}}

        cursor = src.find(query, batch_size=BATCH_SIZE, no_cursor_timeout=True)
        batch = []
        copied = 0
        start_time = time.time()

        try:
            for doc in cursor:
                batch.append(doc)
                if len(batch) >= BATCH_SIZE:
                    dst.insert_many(batch, ordered=False)
                    copied += len(batch)
                    elapsed = time.time() - start_time
                    rate = copied / elapsed if elapsed > 0 else 0
                    logging.info("Worker %d: %s copied (%.0f doc/s)", job_number, f"{copied:,}", rate)
                    batch = []
            if batch:
                dst.insert_many(batch, ordered=False)
                copied += len(batch)
        finally:
            cursor.close()

        elapsed = time.time() - start_time
        logging.info("Worker %d: done — %s docs in %.1f s", job_number, f"{copied:,}", elapsed)
        return {"job_number": job_number, "copied": copied, "seconds": round(elapsed, 1)}
    finally:
        pipeline_client.close()
        frontend_client.close()


def verify_parity(config: dict) -> dict:
    """Verify source and destination counts match for providers and all static collections.

    config:
        env_prefix — database prefix, e.g. "dev"
        states     — list of states to verify (optional, verifies all if omitted)
    """
    pipeline_conn = os.environ.get("MONGO_connectionString")
    frontend_conn = os.environ.get("MONGO_FRONTEND_connectionString")
    if not pipeline_conn or not frontend_conn:
        raise ValueError("Both MONGO_connectionString and MONGO_FRONTEND_connectionString required")

    env_prefix = config.get("env_prefix", "dev")
    db_name = f"{env_prefix}_PublicHealthData" if env_prefix else "PublicHealthData"
    states = config.get("states")

    pipeline_client = MongoClient(pipeline_conn, serverSelectionTimeoutMS=30_000)
    frontend_client = MongoClient(frontend_conn, serverSelectionTimeoutMS=30_000)

    results = []
    all_pass = True
    try:
        src_db = pipeline_client[db_name]
        dst_db = frontend_client[db_name]

        # Check static collections
        for src_coll, dst_coll in _STATIC_COLLECTIONS:
            src_count = src_db[src_coll].count_documents({})
            dst_count = dst_db[dst_coll].count_documents({})
            match = src_count == dst_count
            if not match:
                all_pass = False
            results.append({
                "collection": dst_coll, "source": src_count,
                "destination": dst_count, "parity": match,
            })

        # Check providers (per state if specified)
        if states:
            for state in states:
                q = {"practice_address.state": state}
                src_count = src_db["providers"].count_documents(q)
                dst_count = dst_db["providers"].count_documents(q)
                match = src_count == dst_count
                if not match:
                    all_pass = False
                results.append({
                    "collection": f"providers({state})", "source": src_count,
                    "destination": dst_count, "parity": match,
                })
        else:
            src_count = src_db["providers"].count_documents({})
            dst_count = dst_db["providers"].count_documents({})
            match = src_count == dst_count
            if not match:
                all_pass = False
            results.append({
                "collection": "providers", "source": src_count,
                "destination": dst_count, "parity": match,
            })

        return {"all_pass": all_pass, "collections": results}
    finally:
        pipeline_client.close()
        frontend_client.close()


def run_copy_to_frontend(config: dict) -> dict:
    pipeline_conn  = os.environ.get("MONGO_connectionString")
    frontend_conn  = os.environ.get("MONGO_FRONTEND_connectionString")

    if not pipeline_conn:
        raise ValueError("MONGO_connectionString not set")
    if not frontend_conn:
        raise ValueError("MONGO_FRONTEND_connectionString not set")

    # Caller is responsible for ensuring cluster is IDLE before calling.
    # Use WakeCluster + ClusterStatus via Router to reserve and poll.
    # CopyToFrontEnd assumes cluster is up and does the work.
    env_prefix = config.get("env_prefix", "dev")
    job_id = config.get("job_id", f"copy_to_frontend_{int(time.time())}")

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
        src_db_name = config.get("src_db", f"{os.environ.get('ENV_PREFIX', 'dev')}_PublicHealthData")

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
        # Caller releases reservation via Router Release endpoint

    logging.info("CopyToFrontEnd complete: %s", results)
    return {"status": "complete", "collections": results}
