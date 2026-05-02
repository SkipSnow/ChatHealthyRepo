"""Seed the admin.Versions collection with the first record.

One-shot. Reads the current latest values from
brain/machine_artifacts/content/version.json and writes a single record to
admin.Versions on the front-end MongoDB cluster.

Schema of the record:
    {
        "_id":       <auto>,
        "build":     <int>,
        "version":   <string>,
        "framework": <string>,
        "from":      <iso utc timestamp when this record became active>
    }

Refuses to run if the collection already has any documents — protects
against double-seeding.

Authorization: BUG-001 step "Seed routine".
Implements the canonical-collection direction approved by Skip 2026-05-01.
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / "Code" / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seed_versions")

VERSION_JSON = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "version.json"


def extract_seed_values():
    """Read the latest build/version/framework from version.json."""
    if not VERSION_JSON.exists():
        log.error("version.json not found at %s — cannot seed", VERSION_JSON)
        sys.exit(1)
    with open(VERSION_JSON, encoding="utf-8") as f:
        data = json.load(f)

    versions = data.get("versions", {})
    if isinstance(versions, dict):
        versions = versions.get("version", [])
    if not versions:
        log.error("version.json has no version entries — cannot seed")
        sys.exit(1)

    latest_version = versions[-1]
    version_str = latest_version.get("version_number")
    framework_str = latest_version.get("framework_version")

    builds = latest_version.get("builds", {})
    if isinstance(builds, dict):
        builds = builds.get("build", [])
    if not builds:
        log.error("latest version has no build entries — cannot seed")
        sys.exit(1)

    build_num = builds[-1].get("build_number")
    if build_num is None or version_str is None or framework_str is None:
        log.error("missing required field(s) — build=%s version=%s framework=%s",
                  build_num, version_str, framework_str)
        sys.exit(1)

    return int(build_num), str(version_str), str(framework_str)


def main():
    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        log.error("MONGO_FRONTEND_connectionString not set in environment")
        sys.exit(1)

    build, version, framework = extract_seed_values()
    log.info("Seed values from version.json: build=%d version=%s framework=%s",
             build, version, framework)

    client = MongoClient(conn, serverSelectionTimeoutMS=10000)
    coll = client["admin"]["Versions"]

    existing = coll.count_documents({})
    if existing:
        log.error("admin.Versions already has %d document(s) — refusing to "
                  "seed. Manual intervention required if you intended to "
                  "reseed.", existing)
        sys.exit(2)

    record = {
        "build": build,
        "version": version,
        "framework": framework,
        "from": datetime.now(timezone.utc).isoformat(),
    }
    result = coll.insert_one(record)
    log.info("Seeded admin.Versions with _id=%s: %s", result.inserted_id, record)


if __name__ == "__main__":
    main()
