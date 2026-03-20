# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""migrate_to_new_cluster.py — copies required collections from LearnAIMongoDB
to ChatHealthyDataPipelines.

Collections migrated:
  - PublicHealthData.ZipCountyCrosswalk   (crosswalk reference, ~33k docs)
  - PublicHealthData.providers_staging    (enrichment working data, ~8.8M docs)

Run locally once before firing CountyEnrichment against the new cluster.
Uses a progress counter so you can see how far along the copy is.
"""

import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

from atlas_cluster_manager import scale_up, scale_down

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

LEGACY_CONN = "mongodb+srv://DevUserLearnAI:OldHome7933@learnaimongodb.t7zjiuo.mongodb.net/?appName=LearnAIMongoDB"
NEW_CONN    = os.environ["MONGO_connectionString"]   # ChatHealthyDataPipelines

BATCH_SIZE = 5_000

COLLECTIONS = [
    ("PublicHealthData", "ZipCountyCrosswalk"),
    ("PublicHealthData", "providers_staging"),
]


def migrate_collection(src_db, dst_db, coll_name: str) -> None:
    src = src_db[coll_name]
    dst = dst_db[coll_name]

    total = src.count_documents({})
    existing = dst.count_documents({})
    if existing >= total:
        log.info("%s: already has %s docs — skipping", coll_name, f"{existing:,}")
        return

    log.info("%s: %s docs in source, %s already in dest — migrating remainder",
             coll_name, f"{total:,}", f"{existing:,}")

    # Drop and restart if dest is partially populated
    if existing > 0:
        log.info("%s: dropping partial destination before restart", coll_name)
        dst.drop()

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
                eta = (total - copied) / rate if rate > 0 else 0
                log.info("%s: %s / %s  (%.0f doc/s, ETA %.0f min)",
                         coll_name, f"{copied:,}", f"{total:,}", rate, eta / 60)
                batch = []
        if batch:
            dst.insert_many(batch, ordered=False)
            copied += len(batch)
    finally:
        cursor.close()

    log.info("%s: done — %s docs copied in %.1f min",
             coll_name, f"{copied:,}", (time.time() - start) / 60)


TARGET_CLUSTER = "ChatHealthyDataPipelines"


def main():
    scale_up(TARGET_CLUSTER)

    log.info("Connecting to source: LearnAIMongoDB")
    src_client = MongoClient(LEGACY_CONN, serverSelectionTimeoutMS=30_000)

    log.info("Connecting to destination: ChatHealthyDataPipelines")
    dst_client = MongoClient(NEW_CONN, serverSelectionTimeoutMS=30_000)

    try:
        for db_name, coll_name in COLLECTIONS:
            log.info("=== %s.%s ===", db_name, coll_name)
            migrate_collection(src_client[db_name], dst_client[db_name], coll_name)
    finally:
        src_client.close()
        dst_client.close()

    log.info("Migration complete.")
    scale_down(TARGET_CLUSTER)


if __name__ == "__main__":
    main()
