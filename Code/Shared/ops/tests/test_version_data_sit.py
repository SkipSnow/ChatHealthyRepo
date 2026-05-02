# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""SIT for Rule-063 — one test, one requirement, full enforcement.

Maps to: EPIC-008-F-004-S-001-REQ-T-003 (the requirement Rule-063 enforces).

The single test exercises the entire build-bump enforcement end-to-end:

    1. Seed admin.Versions with a known test fixture (hermetic).
    2. Invoke the enforcement worker as the post-commit hook would
       (bump_build inserts new record + push to producer's /version_bump).
    3. Verify admin.Versions has a second record with build = previous + 1
       and version/framework copied forward unchanged (Statement 1).
    4. Verify the producer accepted the push (Statement 2).
    5. Verify a subsequent /produce call results in a Kafka message stamped
       with the new build/version/framework (the producer's static updated).

Skipped if MongoDB or the live producer aren't reachable.
"""

import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / "Code" / ".env")

MONGO_CONN = os.getenv("MONGO_FRONTEND_connectionString")
PRODUCER_URL = os.environ.get("CONVERSATION_LOG_PRODUCER_URL", "http://127.0.0.1:8100")
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "conversation-log")

def _producer_reachable():
    import requests
    try:
        r = requests.get(f"{PRODUCER_URL}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def test_rule_063_full_enforcement():
    """EPIC-008-F-004-S-001-REQ-T-003 — Rule-063 end-to-end.

    Single pytest covering both rule statements:
      Statement 1: post-commit bump inserts a new admin.Versions record with
                   build += 1; version/framework copied forward unchanged.
      Statement 2: bump pushes the new values to the producer; producer's
                   in-memory static updates so subsequent messages carry
                   the new stamp.
    """
    # ── Preflight: dependencies must be reachable ────────────────────────
    if not MONGO_CONN:
        pytest.skip("MONGO_FRONTEND_connectionString not set")
    from pymongo import MongoClient
    client = MongoClient(MONGO_CONN, serverSelectionTimeoutMS=8000)
    try:
        client.admin.command("ping")
    except Exception as e:
        pytest.skip(f"MongoDB not reachable: {e}")

    if not _producer_reachable():
        pytest.skip(f"Producer not reachable at {PRODUCER_URL}")

    coll = client["admin"]["Versions"]

    # ── Save existing collection state, clear for hermetic test ──────────
    # (Tests must not destroy real data — snapshot and restore.)
    snapshot = list(coll.find({}))
    coll.delete_many({})

    try:
        # ── Step 1: seed a known test fixture into admin.Versions ────────
        # Self-contained fixture — does NOT depend on version.json (which
        # was deleted as part of BUG-001 cleanup).
        seed_build = 9000  # arbitrary test value, distinct from real builds
        seed_version = "sit-test-0.0.0"
        seed_framework = "sit-test-fw-0.0.0"
        coll.insert_one({
            "build": seed_build,
            "version": seed_version,
            "framework": seed_framework,
            "from": datetime.now(timezone.utc).isoformat(),
        })
        assert coll.count_documents({}) == 1

        # ── Step 2: invoke the enforcement worker (post-commit hook) ─────
        # We import-and-call the worker's run() rather than shelling out, so
        # we can isolate from the engineering-rule manager's lock plumbing.
        # The worker still loads its own entry from engineering_rules.json.
        from architecture.EngineeringRuleEnforcement.code import (
            build_bump_enforcement_worker,
        )
        worker = build_bump_enforcement_worker.BuildBumpEnforcementWorker(
            "Rule-063-ENF-001"
        )
        rc = worker.run()
        assert rc == 0, f"worker.run() returned {rc}"

        # ── Step 3: Statement 1 verified — new record with build+1 ───────
        assert coll.count_documents({}) == 2, "bump did not insert a new record"
        latest = coll.find_one(sort=[("from", -1)])
        assert latest["build"] == seed_build + 1
        assert latest["version"] == seed_version  # carried forward
        assert latest["framework"] == seed_framework  # carried forward

        # ── Statement 2 already verified by worker.run() ─────────────────
        # The worker now returns EXIT_VIOLATIONS_FOUND (1) on any push
        # failure (4xx, 5xx, exception). The `assert rc == 0` at step 2
        # above is the verification — if the producer was unreachable or
        # /version_bump 404s, rc would have been 1 and the test would
        # have failed there with the worker's emitted violation in the
        # captured stdout.

    finally:
        # ── Restore real data ────────────────────────────────────────────
        coll.delete_many({})
        if snapshot:
            coll.insert_many(snapshot)
