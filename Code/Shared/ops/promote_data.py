# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Promote provider data from one environment to another on the frontend cluster.
# Copies PublicHealthData (providers + SpecialtyMetaData) and System (build counter).
# Does NOT copy user data (AboutUs, Safety, Debug).
# Usage:
#   python promote_data.py --from dev --to qa
#   python promote_data.py --from dev --to qa --dry-run

import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("promote_data")

BATCH_SIZE = 5000

# Collections to promote (source_collection, destination_collection)
PROMOTE_COLLECTIONS = [
    ("PublicHealthData", "providers"),
    ("PublicHealthData", "SpecialtyMetaData"),
]


def promote_data(from_env: str, to_env: str, dry_run: bool = False):
    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        log.error("MONGO_FRONTEND_connectionString not set")
        sys.exit(1)

    client = MongoClient(conn, serverSelectionTimeoutMS=30000)

    for db_suffix, coll_name in PROMOTE_COLLECTIONS:
        src_db = f"{from_env}_{db_suffix}"
        dst_db = f"{to_env}_{db_suffix}"
        src_col = client[src_db][coll_name]
        dst_col = client[dst_db][coll_name]

        src_count = src_col.estimated_document_count()
        dst_count = dst_col.estimated_document_count()

        log.info("%s.%s: %s docs -> %s.%s: %s docs",
                 src_db, coll_name, f"{src_count:,}",
                 dst_db, coll_name, f"{dst_count:,}")

        if dry_run:
            log.info("  DRY RUN — skipping")
            continue

        if dst_count > 0:
            log.info("  Dropping destination...")
            dst_col.drop()

        log.info("  Copying %s docs...", f"{src_count:,}")
        cursor = src_col.find({}, batch_size=BATCH_SIZE, no_cursor_timeout=True)
        batch = []
        copied = 0
        start = time.time()

        try:
            for doc in cursor:
                batch.append(doc)
                if len(batch) >= BATCH_SIZE:
                    dst_col.insert_many(batch, ordered=False)
                    copied += len(batch)
                    elapsed = time.time() - start
                    rate = copied / elapsed if elapsed > 0 else 0
                    log.info("  %s / %s (%.0f doc/s)", f"{copied:,}", f"{src_count:,}", rate)
                    batch = []
            if batch:
                dst_col.insert_many(batch, ordered=False)
                copied += len(batch)
        finally:
            cursor.close()

        elapsed = time.time() - start
        log.info("  Done: %s docs in %.1f s", f"{copied:,}", elapsed)

    # Per BUG-001: build is global, in admin.Versions, bumped at build time
    # (see DeploymentArchitectureDesignAndMigrationPlanPhase_v14.docx section 2.1 build_chathealthy.py).
    #
    # not per-env. Data promotion does not touch build/version/framework.
    # Log the current global build for the operator's audit trail.
    current_record = client["admin"]["Versions"].find_one(sort=[("from", -1)])
    current_build = current_record["build"] if current_record else "unknown"
    log.info("Current global build: %s (admin.Versions latest record)", current_build)

    log.info("PROMOTE COMPLETE: %s -> %s", from_env, to_env)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Promote provider data between environments")
    parser.add_argument("--from", dest="from_env", required=True,
                        help="Source environment (e.g., dev)")
    parser.add_argument("--to", dest="to_env", required=True,
                        help="Target environment (e.g., qa)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without making changes")
    args = parser.parse_args()

    promote_data(args.from_env, args.to_env, args.dry_run)
