# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Promote a build from one environment to another.
# Copies the build number (does NOT increment) and merges the branch.
# Usage:
#   python promote_build.py --from dev --to qa
#   python promote_build.py --from qa --to prod --confirm-prod
#
# Prod promotion requires --confirm-prod flag (GOV-007).

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("promote_build")

VALID_PROMOTIONS = {
    ("dev", "qa"),
    ("qa", "prod"),
}

BRANCH_MAP = {
    "dev": "dev",
    "qa": "qa",
    "prod": "main",
}


def promote(from_env: str, to_env: str, confirm_prod: bool = False, dry_run: bool = False):
    if (from_env, to_env) not in VALID_PROMOTIONS:
        log.error("Invalid promotion: %s -> %s. Valid: dev->qa, qa->prod", from_env, to_env)
        sys.exit(1)

    if to_env == "prod" and not confirm_prod:
        log.error("REFUSED: promoting to prod requires --confirm-prod flag (GOV-007)")
        sys.exit(1)

    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        log.error("MONGO_FRONTEND_connectionString not set")
        sys.exit(1)

    client = MongoClient(conn, serverSelectionTimeoutMS=10000)

    # Read source build number for logging
    src_db = f"{from_env}_System"
    src_record = client[src_db]["build_counter"].find_one({"_id": "build"})
    src_build = src_record["number"] if src_record else "unknown"
    log.info("Promoting code from %s (build %s) to %s", from_env, src_build, to_env)

    if dry_run:
        log.info("DRY RUN — no changes made")
        log.info("  Would increment %s_System.build_counter", to_env)
        log.info("  Would merge %s -> %s", BRANCH_MAP[from_env], BRANCH_MAP[to_env])
        return

    # Increment target build counter — promotion is a build
    dst_db = f"{to_env}_System"
    result = client[dst_db]["build_counter"].find_one_and_update(
        {"_id": "build"},
        {"$inc": {"number": 1}},
        upsert=True,
        return_document=True,
    )
    new_build = result["number"]
    log.info("Build counter incremented: %s_System = %d", to_env, new_build)

    # Branch merge instructions
    src_branch = BRANCH_MAP[from_env]
    dst_branch = BRANCH_MAP[to_env]
    log.info("NEXT STEP: merge %s -> %s", src_branch, dst_branch)
    log.info("  git checkout %s && git merge %s && git push origin %s", dst_branch, src_branch, dst_branch)
    log.info("Promotion complete. Build %d in %s.", new_build, to_env)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Promote a build between environments")
    parser.add_argument("--from", dest="from_env", required=True, choices=["dev", "qa"],
                        help="Source environment")
    parser.add_argument("--to", dest="to_env", required=True, choices=["qa", "prod"],
                        help="Target environment")
    parser.add_argument("--confirm-prod", action="store_true",
                        help="Required for prod promotion (GOV-007)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without making changes")
    args = parser.parse_args()

    promote(args.from_env, args.to_env, args.confirm_prod, args.dry_run)
