# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# One-shot migration: scalar `build` -> per-env `builds` array on admin.Versions.
#
# Reads the latest admin.Versions record in the front-end cluster's admin DB
# (admin.Versions). If the record already has a `builds` array, exits 0 with
# a notice (idempotent). Otherwise:
#   * carries the scalar `build: int` value into all three env slots
#     [{"env": "dev", "build": N}, {"env": "qa", "build": N}, {"env": "prod", "build": N}]
#   * inserts a NEW record (the collection grows by record; we do not modify
#     the old document in place)
#   * preserves version, framework, and stamps a fresh `from` timestamp.
#
# Run:
#   python Code/Shared/ops/migrate_versions_to_per_env.py
#
# Exit codes:
#   0 = migrated (or already migrated, idempotent no-op)
#   1 = environment / preconditions wrong
#   2 = unexpected data shape in the latest record

import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("migrate_versions_to_per_env")

ENV_ORDER = ("local", "dev", "qa", "prod")


def _looks_like_builds_array(value) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for entry in value:
        if not isinstance(entry, dict):
            return False
        if "env" not in entry or "build" not in entry:
            return False
    return True


def main() -> int:
    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        log.error("MONGO_FRONTEND_connectionString not set in environment")
        return 1

    client = MongoClient(conn, serverSelectionTimeoutMS=10000)
    coll = client["admin"]["Versions"]

    latest = coll.find_one(sort=[("from", -1)])
    if latest is None:
        log.error("admin.Versions has no records; run seed_versions_collection.py first")
        return 1

    if _looks_like_builds_array(latest.get("builds")):
        present = {entry["env"] for entry in latest["builds"]}
        missing = [e for e in ENV_ORDER if e not in present]
        if not missing:
            log.info("admin.Versions latest record already has all required slots; "
                     "nothing to migrate. _id=%s builds=%s",
                     latest.get("_id"), latest.get("builds"))
            return 0
        # Backfill: existing builds preserved; missing envs seeded from dev's
        # current value (so local mirrors dev at seed time, per the per-env
        # counter design).
        existing = {entry["env"]: int(entry["build"]) for entry in latest["builds"]}
        dev_build = existing.get("dev", min(existing.values()) if existing else 0)
        for e in missing:
            existing[e] = dev_build
        new_builds = [{"env": e, "build": existing[e]} for e in ENV_ORDER]
        record = {
            "builds": new_builds,
            "version": latest["version"],
            "framework": latest["framework"],
            "from": datetime.now(timezone.utc).isoformat(),
        }
        result = coll.insert_one(record)
        log.info("Backfilled missing slots %s. New record _id=%s builds=%s",
                 missing, result.inserted_id, new_builds)
        return 0

    scalar_build = latest.get("build")
    if not isinstance(scalar_build, int):
        log.error("latest record has neither a per-env `builds` array nor a "
                  "scalar int `build`; aborting. _id=%s keys=%s",
                  latest.get("_id"), list(latest.keys()))
        return 2

    version = latest.get("version")
    framework = latest.get("framework")
    if version is None or framework is None:
        log.error("latest record is missing version or framework; aborting. "
                  "_id=%s", latest.get("_id"))
        return 2

    new_builds = [{"env": e, "build": int(scalar_build)} for e in ENV_ORDER]
    record = {
        "builds": new_builds,
        "version": version,
        "framework": framework,
        "from": datetime.now(timezone.utc).isoformat(),
    }
    result = coll.insert_one(record)
    log.info("Migrated. New record _id=%s builds=%s version=%s framework=%s "
             "(prior scalar build was %d, _id=%s)",
             result.inserted_id, new_builds, version, framework,
             scalar_build, latest.get("_id"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
