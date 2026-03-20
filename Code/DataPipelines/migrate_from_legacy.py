# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""migrate_from_legacy.py — copies all required collections from LearnAIMongoDB
to the new clusters, running inside Azure (East US 2) for speed.

Destination routing:
  ChatHealthyDataPipelines (MONGO_connectionString):
    - PublicHealthData.ZipCountyCrosswalk
    - PublicHealthData.providers_staging
    - PublicHealthData.SpecialtyMetaData

  ChatHealthyFrontEnd (MONGO_FRONTEND_connectionString):
    - DeidentifiedSessions.unknown_questions  (from AboutUs.AboutSkip)
    - FindCareAdmin.FindCareProductInterfaceCatalouge
    - FindCareAdmin.Incedents

Triggered via POST /api/Router with ChatHealthyTask: MigrateFromLegacy.
Uses MONGO_LEGACY_connectionString (Azure Function App setting) as source.
"""

import logging
import os
import time

from pymongo import MongoClient

BATCH_SIZE = 50_000   # large batches — safe in-region with M30

# (src_db, src_coll, dst_db, dst_coll, dest_cluster)
# dest_cluster: "pipeline" = MONGO_connectionString, "frontend" = MONGO_FRONTEND_connectionString
COLLECTIONS = [
    ("PublicHealthData", "ZipCountyCrosswalk",                  "PublicHealthData",      "ZipCountyCrosswalk",                   "pipeline"),
    ("PublicHealthData", "providers_staging",                   "PublicHealthData",      "providers_staging",                    "pipeline"),
    ("PublicHealthData", "SpecialtyMetaData",                   "PublicHealthData",      "SpecialtyMetaData",                    "pipeline"),
    ("AboutUs",          "AboutSkip",                           "DeidentifiedSessions",  "unknown_questions",                    "frontend"),
    ("FindCareAdmin",    "FindCareProductInterfaceCatalouge",   "FindCareAdmin",         "FindCareProductInterfaceCatalouge",    "frontend"),
    ("FindCareAdmin",    "Incedents",                           "FindCareAdmin",         "Incedents",                            "frontend"),
]


def _migrate_collection(src_db, dst_db, src_coll: str, dst_coll: str = None) -> dict:
    if dst_coll is None:
        dst_coll = src_coll
    src = src_db[src_coll]
    dst = dst_db[dst_coll]
    label = src_coll if src_coll == dst_coll else f"{src_coll}→{dst_coll}"

    total = src.count_documents({})
    existing = dst.count_documents({})

    if existing >= total:
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
                eta_min = (total - copied) / rate / 60 if rate > 0 else 0
                logging.info("%s: %s / %s  (%.0f doc/s, ETA %.1f min)",
                             label, f"{copied:,}", f"{total:,}", rate, eta_min)
                batch = []
        if batch:
            dst.insert_many(batch, ordered=False)
            copied += len(batch)
    finally:
        cursor.close()

    elapsed = time.time() - start
    logging.info("%s: done — %s docs in %.1f min", label, f"{copied:,}", elapsed / 60)
    return {"collection": label, "copied": copied, "elapsed_min": round(elapsed / 60, 1)}


def run_migrate_from_legacy(config: dict) -> dict:
    legacy_conn   = os.environ.get("MONGO_LEGACY_connectionString")
    pipeline_conn = os.environ.get("MONGO_connectionString")
    frontend_conn = os.environ.get("MONGO_FRONTEND_connectionString")

    if not legacy_conn:
        raise ValueError("MONGO_LEGACY_connectionString not set in app settings")

    logging.info("Connecting to source: LearnAIMongoDB")
    src_client      = MongoClient(legacy_conn,   serverSelectionTimeoutMS=30_000)
    pipeline_client = MongoClient(pipeline_conn, serverSelectionTimeoutMS=30_000)
    frontend_client = MongoClient(frontend_conn, serverSelectionTimeoutMS=30_000)

    results = []
    try:
        for src_db, src_coll, dst_db, dst_coll, dest in COLLECTIONS:
            dst_client = pipeline_client if dest == "pipeline" else frontend_client
            label = f"{src_db}.{src_coll} → {dest}:{dst_db}.{dst_coll}"
            logging.info("=== %s ===", label)
            results.append(_migrate_collection(
                src_client[src_db], dst_client[dst_db], src_coll, dst_coll
            ))
    finally:
        src_client.close()
        pipeline_client.close()
        frontend_client.close()

    logging.info("Migration complete: %s", results)
    return {"status": "complete", "collections": results}
