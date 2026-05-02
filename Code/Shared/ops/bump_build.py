# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Bump primitive for the canonical version record.
#
# Implements Statement 1 of Rule-063: on a non-deploy commit, insert a new
# document into admin.Versions with build=last_build+1 and the existing
# version/framework values copied forward. Returns the full new record so
# the caller can push it to the running Kafka producer per Statement 2.
#
# Authorization: BUG-001, Rule-063 (engineering_rules.json).
# Replaces the legacy per-env build_counter pattern.
#
# Importable:
#     from Code.Shared.ops.bump_build import bump
#     new_record = bump()  # {build, version, framework, from}
#
# CLI (for diagnostic use only — production callers use the function):
#     python bump_build.py
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bump_build")


def bump() -> dict:
    """Insert a new version record with build incremented by one.

    Returns:
        The inserted record as a dict with keys: build, version, framework, from.
    Raises:
        RuntimeError: if admin.Versions has no records (seed required first).
        SystemExit: if MONGO_FRONTEND_connectionString is not set.
    """
    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        log.error("MONGO_FRONTEND_connectionString not set")
        sys.exit(1)

    client = MongoClient(conn, serverSelectionTimeoutMS=10000)
    coll = client["admin"]["Versions"]

    latest = coll.find_one(sort=[("from", -1)])
    if latest is None:
        raise RuntimeError(
            "admin.Versions has no records. Run seed_versions_collection.py first."
        )

    record = {
        "build": int(latest["build"]) + 1,
        "version": latest["version"],
        "framework": latest["framework"],
        "from": datetime.now(timezone.utc).isoformat(),
    }
    coll.insert_one(record)
    log.info("Bumped: build=%d version=%s framework=%s",
             record["build"], record["version"], record["framework"])
    # Strip the inserted _id ObjectId so the dict is JSON-serializable for
    # callers that publish it to Kafka.
    return record


if __name__ == "__main__":
    bump()
