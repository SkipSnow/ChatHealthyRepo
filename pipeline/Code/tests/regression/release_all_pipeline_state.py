# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Operator utility: RELEASE ALL pipeline state.

Marks every provider pipeline.runs doc that is still `status='running'`
as `status='aborted_release_all'` and deletes every cluster_lifecycle
reservation held on the pipeline cluster. Used after a batch of stuck
Control ACAs have been killed and we need the runbook gatekeeper to
see a clean slate.

Invocation:
    RELEASE_ALL_PIPELINE_STATE=1 \
    python -m pytest pipeline/Code/tests/regression/release_all_pipeline_state.py -v -s

Opt-in gated on RELEASE_ALL_PIPELINE_STATE=1 so a stray pytest run
against this directory does NOT reset live pipeline state.
"""
from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

import certifi
import pytest
from pymongo import MongoClient

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "pipeline" / "Code"))
from pipeline_env import load_pipeline_env  # noqa: E402


_PIPELINE_CLUSTER_NAME = os.environ.get(
    "PIPELINE_CLUSTER_NAME", "ChatHealthyDataPipelines",
)


def _mongo_client() -> MongoClient:
    load_pipeline_env()
    uri = os.environ.get("MONGO_FRONTEND_connectionString")
    if not uri:
        pytest.fail("MONGO_FRONTEND_connectionString not set")
    return MongoClient(
        uri,
        tlsCAFile=certifi.where(),
        serverSelectionTimeoutMS=15000,
    )


def test_release_all_pipeline_state():
    if os.environ.get("RELEASE_ALL_PIPELINE_STATE", "").strip() != "1":
        pytest.skip("RELEASE_ALL_PIPELINE_STATE not set to 1")

    client = _mongo_client()

    print("[release] before: pipeline.runs status=running for provider:",
          file=sys.stderr, flush=True)
    for r in client["chathealthyfrontend"]["pipeline.runs"].find(
        {"pipeline_name": "provider", "status": "running"},
        {"run_id": 1, "started_at": 1},
    ).limit(20):
        print(f"  {r.get('run_id')} started={r.get('started_at')}",
              file=sys.stderr, flush=True)

    print("[release] before: cluster_lifecycle for pipeline cluster:",
          file=sys.stderr, flush=True)
    for r in client["admin"]["cluster_lifecycle"].find(
        {"cluster_name": _PIPELINE_CLUSTER_NAME},
    ).limit(20):
        print(f"  _id={r.get('_id')} status={r.get('status')} "
              f"requester={r.get('requester')}",
              file=sys.stderr, flush=True)

    aborted_iso = datetime.datetime.utcnow().isoformat()
    abort_result = client["chathealthyfrontend"]["pipeline.runs"].update_many(
        {"pipeline_name": "provider", "status": "running"},
        {"$set": {
            "status": "aborted_release_all",
            "aborted_at": aborted_iso,
        }},
    )
    print(f"[release] aborted runs: matched={abort_result.matched_count} "
          f"modified={abort_result.modified_count}",
          file=sys.stderr, flush=True)

    delete_result = client["admin"]["cluster_lifecycle"].delete_many(
        {"cluster_name": _PIPELINE_CLUSTER_NAME},
    )
    print(f"[release] cleared reservations: deleted={delete_result.deleted_count}",
          file=sys.stderr, flush=True)

    # Post-condition assertions.
    still_running = client["chathealthyfrontend"]["pipeline.runs"].count_documents(
        {"pipeline_name": "provider", "status": "running"},
    )
    assert still_running == 0, (
        f"expected zero running provider runs after release, got {still_running}"
    )
    still_reserved = client["admin"]["cluster_lifecycle"].count_documents(
        {"cluster_name": _PIPELINE_CLUSTER_NAME},
    )
    assert still_reserved == 0, (
        f"expected zero pipeline cluster reservations after release, "
        f"got {still_reserved}"
    )
    print("[release] verified clean slate", file=sys.stderr, flush=True)
