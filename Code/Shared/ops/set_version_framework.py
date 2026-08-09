# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Human-driven update of version and/or framework in admin.Versions.

Per BUG-001: build is global, in admin.Versions, and is bumped at build time
(see DeploymentArchitectureDesignAndMigrationPlanPhase_v14.docx section 2.1 build_chathealthy.py).
version and framework are set ONLY by humans, only at
prod UAT sign-off. Claude invokes this routine when the operator authorizes
a version or framework update.

Pattern matches the build-time bump in build_chathealthy.py:
    1. Read latest admin.Versions record.
    2. Insert a new record with the supplied version and/or framework, the
       previous `builds` array copied forward unchanged (per-env build slots
       are not touched by this routine), and a fresh `from` timestamp.

Usage:
    python set_version_framework.py --version 0.5.0
    python set_version_framework.py --framework 0.2.0
    python set_version_framework.py --version 0.5.0 --framework 0.2.0

At least one of --version or --framework MUST be supplied.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from chathealthy_frontend_lib.exceptions import ChatHealthyException
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("set_version_framework")

PUSH_TIMEOUT_SECONDS = 5


VALID_ENVS = ("local", "dev", "qa", "prod")


def _carry_forward_builds(builds_array):
    """Return the input builds array filtered + ordered. No values change."""
    by_env = {}
    for entry in builds_array or []:
        env = entry.get("env")
        if env in VALID_ENVS:
            by_env[env] = int(entry["build"])
    return [{"env": e, "build": by_env[e]} for e in VALID_ENVS if e in by_env]


def set_version_framework(version: str | None, framework: str | None) -> dict:
    """Insert a new admin.Versions record with the supplied changes.

    The `builds` array is copied forward unchanged from the latest record;
    this routine only updates version and/or framework.

    Returns the inserted record dict (builds, version, framework, from).
    """
    if version is None and framework is None:
        raise ChatHealthyException(
            mode="value_error",
            component="SetVersionFramework",
            message="must supply at least one of version, framework")

    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        log.error("MONGO_FRONTEND_connectionString not set")
        sys.exit(1)

    client = MongoClient(conn, serverSelectionTimeoutMS=10000)
    coll = client["admin"]["Versions"]
    latest = coll.find_one(sort=[("from", -1)])
    if latest is None:
        raise ChatHealthyException(
            mode="versions_collection_empty",
            component="SetVersionFramework",
            message="admin.Versions has no records. Run "
                    "seed_versions_collection.py first.")

    carried_builds = _carry_forward_builds(latest.get("builds", []))
    if not carried_builds:
        raise ChatHealthyException(
            mode="versions_record_missing_builds",
            component="SetVersionFramework",
            message="latest admin.Versions record has no per-env builds "
                    "array; run migrate_versions_to_per_env.py first.")

    record = {
        "builds": carried_builds,  # preserved
        "version": version if version is not None else latest["version"],
        "framework": framework if framework is not None else latest["framework"],
        "from": datetime.now(timezone.utc).isoformat(),
    }
    coll.insert_one(record)
    log.info("Inserted: builds=%s version=%s framework=%s",
             record["builds"], record["version"], record["framework"])
    return record




def main():
    parser = argparse.ArgumentParser(
        description="Update version and/or framework in admin.Versions"
    )
    parser.add_argument("--version", help="New version ID (e.g., 0.5.0)")
    parser.add_argument("--framework", help="New framework ID (e.g., 0.2.0)")
    args = parser.parse_args()

    if not args.version and not args.framework:
        parser.error("must supply at least one of --version, --framework")

    record = set_version_framework(args.version, args.framework)


if __name__ == "__main__":
    main()
