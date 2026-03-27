# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).

"""move_to_frontend.py — copy databases from DataPipelines cluster to FrontEnd
cluster as dev_{dbname}, then drop them from DataPipelines.

Idempotent copy: skips collections where dst already has >= src doc count.
Drop only happens after a successful copy.

Usage:
    python move_to_frontend.py [--dry-run]
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
log = logging.getLogger("move_to_frontend")

# Databases to move: src name on DataPipelines → dst name on FrontEnd
MOVES = {
    "Safety": "dev_Safety",
}

BATCH_SIZE = 10_000


def copy_collection(src_col, dst_col, dry_run: bool) -> int:
    src_count = src_col.count_documents({})
    dst_count = dst_col.count_documents({})

    if dst_count >= src_count > 0:
        log.info("  SKIP %s — dst already has %d docs", dst_col.name, dst_count)
        return src_count  # treat as success

    log.info("  COPY %s → %s  (%d docs)", src_col.full_name, dst_col.full_name, src_count)
    if dry_run:
        return src_count

    if dst_count > 0:
        dst_col.drop()

    if src_count == 0:
        return 0

    cursor = src_col.find({}, batch_size=BATCH_SIZE, no_cursor_timeout=True)
    batch, copied = [], 0
    try:
        for doc in cursor:
            batch.append(doc)
            if len(batch) >= BATCH_SIZE:
                dst_col.insert_many(batch, ordered=False)
                copied += len(batch)
                log.info("    %d / %d", copied, src_count)
                batch = []
        if batch:
            dst_col.insert_many(batch, ordered=False)
            copied += len(batch)
    finally:
        cursor.close()

    log.info("  DONE — %d docs copied", copied)
    return copied


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    pipeline_conn  = os.environ.get("MONGO_connectionString")
    frontend_conn  = os.environ.get("MONGO_FRONTEND_connectionString")

    if not pipeline_conn:
        log.error("MONGO_connectionString not set")
        sys.exit(1)
    if not frontend_conn:
        log.error("MONGO_FRONTEND_connectionString not set")
        sys.exit(1)

    pipeline_client = MongoClient(pipeline_conn,  serverSelectionTimeoutMS=30_000)
    frontend_client = MongoClient(frontend_conn,  serverSelectionTimeoutMS=30_000)

    pipeline_dbs = pipeline_client.list_database_names()
    log.info("DataPipelines databases: %s", pipeline_dbs)

    for src_name, dst_name in MOVES.items():
        if src_name not in pipeline_dbs:
            log.info("DB %s not found on DataPipelines — skipping", src_name)
            continue

        log.info("=== %s (DataPipelines) → %s (FrontEnd) ===", src_name, dst_name)
        src_db  = pipeline_client[src_name]
        dst_db  = frontend_client[dst_name]
        colls   = src_db.list_collection_names()

        all_ok = True
        for coll_name in colls:
            copied = copy_collection(src_db[coll_name], dst_db[coll_name], args.dry_run)
            src_count = src_db[coll_name].count_documents({})
            if copied < src_count:
                log.error("  COPY INCOMPLETE for %s (%d / %d) — will NOT drop source", coll_name, copied, src_count)
                all_ok = False

        if all_ok and not args.dry_run:
            log.info("Dropping %s from DataPipelines...", src_name)
            pipeline_client.drop_database(src_name)
            log.info("Dropped %s from DataPipelines.", src_name)
        elif args.dry_run:
            log.info("DRY RUN — would drop %s from DataPipelines after copy", src_name)

    pipeline_client.close()
    frontend_client.close()
    log.info("Done.")


if __name__ == "__main__":
    main()
