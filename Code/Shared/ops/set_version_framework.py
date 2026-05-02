# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Human-driven update of version and/or framework in admin.Versions.

Per BUG-001 and Rule-063: build is auto-incremented on every
non-deploy commit. version and framework are set ONLY by humans, only at
prod UAT sign-off. Claude invokes this routine when Skip authorizes a
version or framework update.

Pattern matches the post-commit bump:
    1. Read latest admin.Versions record.
    2. Insert a new record with the supplied version and/or framework, the
       previous build (unchanged), and a fresh `from` timestamp.
    3. POST {build, version, framework} to the conversation-log producer's
       /version_bump endpoint so its in-memory static updates.

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

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("set_version_framework")

PRODUCER_URL = os.environ.get(
    "CONVERSATION_LOG_PRODUCER_URL", "http://127.0.0.1:8100"
)
PUSH_TIMEOUT_SECONDS = 5


def set_version_framework(version: str | None, framework: str | None) -> dict:
    """Insert a new admin.Versions record with the supplied changes.

    Returns the inserted record dict (build, version, framework, from).
    """
    if version is None and framework is None:
        raise ValueError("must supply at least one of version, framework")

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
        "build": int(latest["build"]),  # preserved
        "version": version if version is not None else latest["version"],
        "framework": framework if framework is not None else latest["framework"],
        "from": datetime.now(timezone.utc).isoformat(),
    }
    coll.insert_one(record)
    log.info("Inserted: build=%d version=%s framework=%s",
             record["build"], record["version"], record["framework"])
    return record


def push_to_producer(record: dict) -> None:
    """POST {build, version, framework} to the producer's /version_bump."""
    try:
        import requests

        payload = {
            "build": record["build"],
            "version": record["version"],
            "framework": record["framework"],
        }
        resp = requests.post(
            f"{PRODUCER_URL}/version_bump",
            json=payload,
            timeout=PUSH_TIMEOUT_SECONDS,
        )
        if resp.status_code == 200:
            log.info("Pushed to producer at %s", PRODUCER_URL)
        else:
            log.warning("/version_bump returned %d: %s",
                        resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("push to producer failed (non-fatal): %s: %s",
                    type(exc).__name__, exc)


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
    push_to_producer(record)


if __name__ == "__main__":
    main()
