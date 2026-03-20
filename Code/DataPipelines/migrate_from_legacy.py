# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""migrate_from_legacy.py — copies required collections from LearnAIMongoDB
to ChatHealthyDataPipelines, running inside Azure (East US 2) for speed.

Collections migrated:
  - PublicHealthData.ZipCountyCrosswalk
  - PublicHealthData.providers_staging

Triggered via POST /api/Router with ChatHealthyTask: MigrateFromLegacy.
Uses MONGO_LEGACY_connectionString (Azure Function App setting) as source.
Uses MONGO_connectionString as destination (ChatHealthyDataPipelines).
"""

import logging
import os
import time

from pymongo import MongoClient

BATCH_SIZE = 50_000   # large batches — safe in-region with M30

COLLECTIONS = [
    ("PublicHealthData", "ZipCountyCrosswalk"),
    ("PublicHealthData", "providers_staging"),
]


def _migrate_collection(src_db, dst_db, coll_name: str) -> dict:
    src = src_db[coll_name]
    dst = dst_db[coll_name]

    total = src.count_documents({})
    existing = dst.count_documents({})

    if existing >= total:
        logging.info("%s: already has %s docs — skipping", coll_name, f"{existing:,}")
        return {"collection": coll_name, "copied": 0, "skipped": True}

    if existing > 0:
        logging.info("%s: dropping partial destination (%s docs) before restart", coll_name, f"{existing:,}")
        dst.drop()

    logging.info("%s: copying %s docs", coll_name, f"{total:,}")
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
                eta_min = (total - copied) / rate / 60 if rate > 0 else 0
                logging.info("%s: %s / %s  (%.0f doc/s, ETA %.1f min)",
                             coll_name, f"{copied:,}", f"{total:,}", rate, eta_min)
                batch = []
        if batch:
            dst.insert_many(batch, ordered=False)
            copied += len(batch)
    finally:
        cursor.close()

    elapsed = time.time() - start
    logging.info("%s: done — %s docs in %.1f min", coll_name, f"{copied:,}", elapsed / 60)
    return {"collection": coll_name, "copied": copied, "elapsed_min": round(elapsed / 60, 1)}


def run_migrate_from_legacy(config: dict) -> dict:
    legacy_conn = os.environ.get("MONGO_LEGACY_connectionString")
    new_conn    = os.environ.get("MONGO_connectionString")

    if not legacy_conn:
        raise ValueError("MONGO_LEGACY_connectionString not set in app settings")

    logging.info("Connecting to source: LearnAIMongoDB")
    src_client = MongoClient(legacy_conn, serverSelectionTimeoutMS=30_000)

    logging.info("Connecting to destination: ChatHealthyDataPipelines")
    dst_client = MongoClient(new_conn, serverSelectionTimeoutMS=30_000)

    results = []
    try:
        for db_name, coll_name in COLLECTIONS:
            results.append(_migrate_collection(src_client[db_name], dst_client[db_name], coll_name))
    finally:
        src_client.close()
        dst_client.close()

    logging.info("Migration complete: %s", results)
    return {"status": "complete", "collections": results}
