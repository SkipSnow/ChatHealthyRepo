# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).

"""rename_to_dev.py — copy all collections from unprefixed databases to
dev_{dbname} equivalents on the FrontEnd cluster.

Idempotent: skips collections where destination already has >= source doc count.
Does NOT drop the source databases — run that manually once verified.

Usage:
    python rename_to_dev.py [--dry-run]
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
log = logging.getLogger("rename_to_dev")

# Databases to copy to dev_ equivalents (source → destination)
# Add more here if the cluster has additional databases.
DATABASES_TO_PREFIX = [
    "PublicHealthData",
    "AboutUs",
    "Safety",
    "Brain",
    "Backlog",
]

BATCH_SIZE = 10_000


def copy_collection(src_col, dst_col, dry_run: bool) -> dict:
    src_count = src_col.count_documents({})
    dst_count = dst_col.count_documents({})

    if dst_count >= src_count > 0:
        log.info("  SKIP %s — dst already has %d docs (src: %d)", dst_col.name, dst_count, src_count)
        return {"collection": dst_col.name, "copied": 0, "skipped": True}

    log.info("  COPY %s → %s  (%d docs)", src_col.name, dst_col.full_name, src_count)
    if dry_run:
        return {"collection": dst_col.name, "copied": src_count, "dry_run": True}

    if dst_count > 0:
        log.info("  DROP existing dst (%d docs)", dst_count)
        dst_col.drop()

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

    log.info("  DONE %s — %d docs copied", dst_col.name, copied)
    return {"collection": dst_col.name, "copied": copied}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Show what would be copied without writing")
    args = parser.parse_args()

    conn = os.environ.get("MONGO_FRONTEND_connectionString")
    if not conn:
        log.error("MONGO_FRONTEND_connectionString not set")
        sys.exit(1)

    client = MongoClient(conn, serverSelectionTimeoutMS=30_000)
    existing_dbs = client.list_database_names()
    log.info("Existing databases: %s", existing_dbs)

    results = []
    for db_name in DATABASES_TO_PREFIX:
        if db_name not in existing_dbs:
            log.info("DB %s not found — skipping", db_name)
            continue

        dst_db_name = f"dev_{db_name}"
        log.info("=== %s → %s ===", db_name, dst_db_name)

        src_db = client[db_name]
        dst_db = client[dst_db_name]

        for coll_name in src_db.list_collection_names():
            result = copy_collection(src_db[coll_name], dst_db[coll_name], args.dry_run)
            results.append({**result, "src_db": db_name, "dst_db": dst_db_name})

    log.info("=== Summary ===")
    for r in results:
        status = "DRY RUN" if r.get("dry_run") else ("SKIPPED" if r.get("skipped") else f"COPIED {r['copied']} docs")
        log.info("  %s.%s → %s.%s — %s", r["src_db"], r["collection"], r["dst_db"], r["collection"], status)

    client.close()


if __name__ == "__main__":
    main()
