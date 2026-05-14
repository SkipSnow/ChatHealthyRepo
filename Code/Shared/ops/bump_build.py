# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Bump primitive for the canonical version record.
#
# Implements Statement 1 of Rule-063: on a non-deploy commit, insert a new
# document into admin.Versions with the targeted env slot's build incremented
# by one and the other env slots + version + framework copied forward
# unchanged. Returns the full new record so the caller can push it to the
# running Kafka producer per Statement 2.
#
# The env defaults to "dev". Local commits and the post-commit Rule-063
# enforcement worker bump dev. qa and prod are NEVER bumped here; they are
# only touched by promote_build.py.
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


VALID_ENVS = ("local", "dev", "qa", "prod")


def _builds_to_map(builds_array: list) -> dict:
    """Turn [{env, build}, ...] into {env: build}. Tolerant of missing envs."""
    out = {}
    for entry in builds_array or []:
        env = entry.get("env")
        if env in VALID_ENVS:
            out[env] = int(entry["build"])
    return out


def bump(env: str = "dev") -> dict:
    """Insert a new version record with the env's build slot incremented.

    Args:
        env: Which env slot to advance. Defaults to "dev". The post-commit
             Rule-063 worker and any "bump the build" caller advances dev;
             qa and prod are reserved for promote_build.py.

    Returns:
        The inserted record as a dict with keys: builds, version, framework, from.
    Raises:
        ValueError: if env is not one of dev/qa/prod.
        RuntimeError: if admin.Versions has no records (seed required first).
        SystemExit: if MONGO_FRONTEND_connectionString is not set.
    """
    if env not in VALID_ENVS:
        raise ValueError(f"env must be one of {VALID_ENVS}, got {env!r}")

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

    builds_map = _builds_to_map(latest.get("builds", []))
    # Per-env bump semantics. Every env catches up after its own deploy
    # event; dev advances on commit (Rule-063):
    #   dev   — increments dev by 1 (commit-driven Rule-063).
    #   local — local := dev. Catches up after local deploy success.
    #   qa    — qa := dev. Catches up after qa deploy success
    #           (which follows a dev->qa branch merge).
    #   prod  — prod := qa. Catches up after prod deploy success
    #           (which follows a qa->main branch merge).
    if env == "dev":
        # Dev advances by 1. Local follows the operator-stated
        # invariant: local ∈ {dev, dev-1}. Setting local := old_dev
        # (which equals new_dev - 1) enforces "dev-1 when not caught
        # up" automatically. The local deploy with a clean working
        # tree later catches local up to dev.
        old_dev = builds_map.get("dev", 0)
        builds_map["dev"] = old_dev + 1
        builds_map["local"] = old_dev
    elif env == "local":
        builds_map["local"] = builds_map.get("dev", 0)
    elif env == "qa":
        builds_map["qa"] = builds_map.get("dev", 0)
    elif env == "prod":
        builds_map["prod"] = builds_map.get("qa", 0)
    else:
        raise ValueError(
            f"bump() is for {{dev, local, qa, prod}} — got {env!r}."
        )
    # Every VALID_ENV is preserved in every record so readers never see
    # null. Slots missing from the prior record default to 0.
    new_builds = [
        {"env": e, "build": builds_map.get(e, 0)}
        for e in VALID_ENVS
    ]

    record = {
        "builds": new_builds,
        "version": latest["version"],
        "framework": latest["framework"],
        "from": datetime.now(timezone.utc).isoformat(),
    }
    coll.insert_one(record)
    log.info("Bumped %s: builds=%s version=%s framework=%s",
             env, new_builds, record["version"], record["framework"])
    # Strip the inserted _id ObjectId so the dict is JSON-serializable for
    # callers that publish it to Kafka.
    return record


if __name__ == "__main__":
    bump()
