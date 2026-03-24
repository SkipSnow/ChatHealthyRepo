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

# (src_db, src_coll, dst_db, dst_coll)
COLLECTIONS = [
    ("PublicHealthData", "SpecialtyMetaData", "PublicHealthData", "SpecialtyMetaData"),
]


def _copy_collection(src_db, dst_db, src_coll: str, dst_coll: str) -> dict:
    src = src_db[src_coll]
    dst = dst_db[dst_coll]
    label = src_coll if src_coll == dst_coll else f"{src_coll}→{dst_coll}"

    total = src.count_documents({})
    existing = dst.count_documents({})

    if existing >= total and total > 0:
        logging.info("%s: already has %s docs — skipping", label, f"{existing:,}")
        return {"collection": label, "copied": 0, "skipped": True}

    if existing > 0:
        logging.info("%s: dropping partial destination (%s docs) before restart", label, f"{existing:,}")
        dst.drop()

    logging.info("%s: copying %s docs", label, f"{total:,}")
    cursor = src.find({}, batch_size=BATCH_SIZE, no_cursor_timeout=True)
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
    source = config.get("source", "providers_staging")
    destination = config.get("destination")
    if not destination:
        raise ValueError("snapshot: 'destination' collection name is required in payload")

    conn = os.environ.get("MONGO_connectionString")
    if not conn:
        raise ValueError("MONGO_connectionString not set")

    client = MongoClient(conn, serverSelectionTimeoutMS=30_000)
    try:
        db = client["PublicHealthData"]
        count_before = db[source].count_documents({})
        logging.info("Snapshot: %s (%s docs) → %s", source, f"{count_before:,}", destination)
        list(db[source].aggregate([{"$out": destination}], allowDiskUse=True))
        count_after = db[destination].count_documents({})
        logging.info("Snapshot complete: %s → %s (%s docs)", source, destination, f"{count_after:,}")
        return {"source": source, "destination": destination, "source_count": count_before, "copied": count_after}
    finally:
        client.close()


def run_copy_to_frontend(config: dict) -> dict:
    pipeline_conn  = os.environ.get("MONGO_connectionString")
    frontend_conn  = os.environ.get("MONGO_FRONTEND_connectionString")

    if not pipeline_conn:
        raise ValueError("MONGO_connectionString not set")
    if not frontend_conn:
        raise ValueError("MONGO_FRONTEND_connectionString not set")

    pipeline_client = MongoClient(pipeline_conn,  serverSelectionTimeoutMS=30_000)
    frontend_client = MongoClient(frontend_conn,  serverSelectionTimeoutMS=30_000)

    results = []
    try:
        for src_db, src_coll, dst_db, dst_coll in COLLECTIONS:
            logging.info("=== %s.%s → frontend:%s.%s ===", src_db, src_coll, dst_db, dst_coll)
            results.append(_copy_collection(
                pipeline_client[src_db], frontend_client[dst_db], src_coll, dst_coll
            ))
    finally:
        pipeline_client.close()
        frontend_client.close()

    logging.info("CopyToFrontEnd complete: %s", results)
    return {"status": "complete", "collections": results}
