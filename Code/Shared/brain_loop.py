# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""
Brain Loop — ChatHealthy Binary Operating Model.

Implements the Claude ↔ GPT autonomous review loop defined in the
ChatHealthy Operational Model Specification (framework_02, 2026-03-25).

Flow:
  0. Boss writes assignment to brain/assignment_queue.json (write_assignment)
  1. Claude picks up assignment (pick_up_assignment) → reads from assignment_queue.json
  2. Claude completes implementation work
  3. Claude calls write_review_pack() → writes to brain/review_queue.json
  4. GPT reads review_queue.json, writes to brain/assurance_results.json
  5. Claude calls read_assurance_results() → gets UAT scenarios + gate decision
  6. Claude calls run_uat() → executes all UAT scenarios
  7. Claude calls close_review() → marks complete or escalates

Gate Rules (from spec):
  Low       → auto-proceed
  Moderate  → proceed with warning logged
  High      → escalate to Boss
  Critical  → block + escalate to Boss
  Suicidal  → block + Boss required in-session

Files (repo root /brain/):
  assignment_queue.json  — Boss writes assignments here
  review_queue.json      — Claude writes Review Packs here
  assurance_results.json — GPT writes Assurance Output here
  execution_state.json   — live loop state
  uat_library.json       — all UAT scenarios (become regression on pass)

ADR: ADR-0007 (Machine Brain), MB-0001 (Boss Governance), MB-0010 (Release Manifest)
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from pymongo import MongoClient
    _PYMONGO = True
except ImportError:
    _PYMONGO = False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_BRAIN_DIR = _REPO_ROOT / "brain"

_ASSIGNMENT_QUEUE = _BRAIN_DIR / "assignment_queue.json"
_REVIEW_QUEUE     = _BRAIN_DIR / "review_queue.json"
_ASSURANCE        = _BRAIN_DIR / "assurance_results.json"
_EXEC_STATE       = _BRAIN_DIR / "execution_state.json"
_UAT_LIBRARY      = _BRAIN_DIR / "uat_library.json"


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------

GATE_RULES = {
    "Low":      "auto",
    "Moderate": "proceed_with_warning",
    "High":     "escalate",
    "Critical": "block_escalate",
    "Suicidal": "block_boss_required",
}

AUTO_GATES   = {"auto", "proceed_with_warning"}
BLOCK_GATES  = {"block_escalate", "block_boss_required"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    _mongo_sync(path, data)


# ---------------------------------------------------------------------------
# Blob storage — write Brain artifacts to Azure Blob Storage (best-effort)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# MongoDB sync — dual-write after every JSON write (best-effort)
# ---------------------------------------------------------------------------

_ENV_PREFIX = os.getenv("ENV_PREFIX", "dev")
_BRAIN_DB   = f"{_ENV_PREFIX}_Brain"
_mongo_client_cache: dict = {}


def _get_mongo_db():
    if not _PYMONGO:
        return None
    # Brain lives on the always-on FrontEnd cluster, not DataPipelines
    uri = os.getenv("MONGO_FRONTEND_connectionString") or os.getenv("MONGO_connectionString")
    if not uri:
        return None
    try:
        if "client" not in _mongo_client_cache:
            _mongo_client_cache["client"] = MongoClient(uri, serverSelectionTimeoutMS=3000)
        return _mongo_client_cache["client"][_BRAIN_DB]
    except Exception:
        return None


def _upsert_many(collection, docs: list, id_field: str) -> None:
    for doc in docs:
        if id_field in doc:
            collection.replace_one({id_field: doc[id_field]}, doc, upsert=True)


def _mongo_sync(path: Path, data: dict) -> None:
    """Sync written data to MongoDB. Best-effort — never raises."""
    db = _get_mongo_db()
    if db is None:
        return
    try:
        if path == _ASSIGNMENT_QUEUE:
            _upsert_many(db["assignments"], data.get("assignments", []), "assignment_id")
        elif path == _REVIEW_QUEUE:
            _upsert_many(db["reviews"], data.get("reviews", []), "review_id")
        elif path == _ASSURANCE:
            _upsert_many(db["assurance_results"], data.get("results", []), "review_id")
        elif path == _EXEC_STATE:
            db["execution_state"].replace_one(
                {"_singleton": True}, {**data, "_singleton": True}, upsert=True
            )
        elif path == _UAT_LIBRARY:
            _upsert_many(db["uat_scenarios"], data.get("scenarios", []), "scenario_id")
    except Exception as e:
        print(f"[BrainLoop] WARNING: MongoDB sync failed — {e}", flush=True)


def _set_state(review_id: Optional[str], status: str, escalation_reason: Optional[str] = None) -> None:
    state = _read(_EXEC_STATE)
    state["current_review_id"] = review_id
    state["loop_status"] = status
    state["last_updated"] = _now()
    if escalation_reason is not None:
        state["escalation_reason"] = escalation_reason
    _write(_EXEC_STATE, state)


def _machine_brain_context(summary: str) -> list[dict]:
    """Pull relevant Machine Brain records to include in the Review Pack."""
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from machine_brain import semantic_search
        results = semantic_search(summary, top_k=5)
        # Strip embeddings — they are large and not useful in the review pack
        return [{k: v for k, v in r.items() if k != "embedding"} for r in results]
    except Exception as e:
        print(f"[BrainLoop] WARNING: machine_brain query failed — {e}", flush=True)
        return []


# ---------------------------------------------------------------------------
# Step 0 — Boss writes assignment
# ---------------------------------------------------------------------------

def list_assignments(status: Optional[str] = None) -> list[dict]:
    """
    Return assignments, optionally filtered by status.
    Status values: pending | in_progress | complete | escalated
    """
    queue = _read(_ASSIGNMENT_QUEUE)
    assignments = queue.get("assignments", [])
    if status:
        assignments = [a for a in assignments if a.get("status") == status]
    return assignments


# ---------------------------------------------------------------------------
# Agent → Boss: deliver result or request feedback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Step 1 — Claude writes Review Pack
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Step 4 — Claude reads GPT's Assurance Output
# ---------------------------------------------------------------------------

def read_assurance_results(review_id: str) -> Optional[dict]:
    """
    Claude calls this to retrieve GPT's assurance output for a review.

    Returns None if GPT has not yet written results for this review_id.
    Returns the assurance result dict if available.

    Assurance result fields:
        architecture_status:  pass | fail
        behavior_status:      pass | fail
        risk:                 Low | Moderate | High | Critical | Suicidal
        issues:               list of issue descriptions
        uat_scenarios:        list of UAT scenario dicts
        gate_recommendation:  auto | proceed_with_warning | escalate |
                              block_escalate | block_boss_required

    Security: gpt_api_key must NEVER appear in Assurance Output JSON.
    Keys exist only in the agent runtime — never in files, schemas, or logs.
    """
    data = _read(_ASSURANCE)
    for result in data.get("results", []):
        # Strip any accidentally-included key fields before processing
        result.pop("gpt_api_key", None)
        result.pop("bearer_token", None)
        if result.get("review_id") == review_id:
            risk = result.get("risk", "High")
            gate = result.get("gate_recommendation", GATE_RULES.get(risk, "escalate"))

            if gate in BLOCK_GATES:
                reason = f"Gate '{gate}' for review {review_id} — risk: {risk}"
                _set_state(review_id, "blocked", reason)
                print(f"[BrainLoop] BLOCKED: {reason}", flush=True)
                if gate == "block_boss_required":
                    print(f"[BrainLoop] Boss sign-off required before proceeding.", flush=True)
            elif gate == "proceed_with_warning":
                _set_state(review_id, "testing")
                print(f"[BrainLoop] WARNING: Moderate risk — proceeding with caution.", flush=True)
            elif gate == "escalate":
                reason = f"High risk escalation for review {review_id}"
                _set_state(review_id, "escalated", reason)
                print(f"[BrainLoop] ESCALATED to Boss: {reason}", flush=True)
            else:
                _set_state(review_id, "testing")

            return result

    return None


# ---------------------------------------------------------------------------
# Step 5 — Claude runs UAT
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Step 6 — Close review
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Utility — regression run
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Utility — current state
# ---------------------------------------------------------------------------


