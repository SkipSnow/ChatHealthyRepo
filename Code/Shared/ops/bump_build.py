# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Increment the system-wide build counter in MongoDB.
# Called by every deploy workflow (website, pipeline, chat) and locally.
# Usage:
#   python bump_build.py --env dev
#   python bump_build.py --env qa
#   python bump_build.py --env prod

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bump_build")


def bump(env: str) -> int:
    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        log.error("MONGO_FRONTEND_connectionString not set")
        sys.exit(1)

    client = MongoClient(conn, serverSelectionTimeoutMS=10000)
    db_name = f"{env}_System"
    result = client[db_name]["build_counter"].find_one_and_update(
        {"_id": "build"},
        {"$inc": {"number": 1}},
        upsert=True,
        return_document=True,
    )
    build = result["number"]
    log.info("Build %d (env=%s)", build, env)
    return build


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Increment system build counter")
    parser.add_argument("--env", required=True, choices=["dev", "qa", "prod"],
                        help="Environment: dev, qa, or prod")
    args = parser.parse_args()
    bump(args.env)
