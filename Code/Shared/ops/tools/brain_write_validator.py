# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# brain_write_validator.py — BRAIN-RUNTIME-REQ-002, BRAIN-RUNTIME-REQ-003.
#
# Validates proposed writes to bugs.json and agile_backlog.json:
#   - Semantic duplicate detection for bugs (before insert).
#   - Orphan-bug handling: bugs without a req_id are allowed only with
#     mode-specific handling (Unattended: mark orphan; Normal: escalate;
#     Idiot: refuse).
#   - Duplicate-requirement detection for agile_backlog.
#
# All logic speaks the new ChatHealthyBugsSchema v1 shape:
#   top-level key "bug" (array); per-record keys bugid, title,
#   description, reproduction, req_id, status, ...

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path

BRAIN_DIR = Path(__file__).resolve().parents[4] / "brain" / "machine_artifacts" / "content"
SIMILARITY_THRESHOLD = 0.70
MODES = ("unattended", "normal", "idiot")

RUNTIME_BRAIN_FILES = ("bugs.json", "engineering_rules.json", "agile_backlog.json")


# ── similarity primitives ────────────────────────────────────────────────

def _text_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _bug_text(bug: dict) -> str:
    """Canonical text blob for duplicate detection: the three free-text
    fields in the new schema."""
    return " ".join(filter(None, [
        (bug.get("title") or "").strip(),
        (bug.get("description") or "").strip(),
        (bug.get("reproduction") or "").strip(),
    ]))


def find_duplicate_bug(proposed_text: str, bugs_list: list,
                       threshold: float = SIMILARITY_THRESHOLD) -> dict | None:
    """Return {'match': bug, 'score': float} for the closest existing bug
    whose similarity to proposed_text meets or exceeds threshold, else None."""
    if not bugs_list:
        return None
    best = None
    best_score = 0.0
    for bug in bugs_list:
        if not isinstance(bug, dict):
            continue
        score = _text_similarity(proposed_text, _bug_text(bug))
        if score > best_score:
            best_score = score
            best = bug
    if best is not None and best_score >= threshold:
        return {"match": best, "score": best_score}
    return None


def find_duplicate_req(proposed_text: str, reqs_list: list,
                       threshold: float = SIMILARITY_THRESHOLD) -> dict | None:
    """Return {'match': req, 'score': float} for the closest existing
    requirement meeting threshold, else None."""
    if not reqs_list:
        return None
    best = None
    best_score = 0.0
    for req in reqs_list:
        if not isinstance(req, dict):
            continue
        score = _text_similarity(proposed_text, req.get("requirement") or "")
        if score > best_score:
            best_score = score
            best = req
    if best is not None and best_score >= threshold:
        return {"match": best, "score": best_score}
    return None


# ── insert validation ────────────────────────────────────────────────────

def validate_bug_insert(proposed: dict, mode: str,
                        bugs_list: list | None = None) -> dict:
    """Validate a proposed bug record per BRAIN-RUNTIME-REQ-002.

    Returns:
        {
            'ok': bool,
            'reason': str,
            'action': 'INSERT' | 'UPDATE <bugid>' | 'ESCALATE' |
                      'INSERT_AS_ORPHAN' | 'REFUSE',
            'match_id': str,     # existing bugid if duplicate found
            'match_score': float # optional, only on duplicate match
        }
    """
    if mode not in MODES:
        raise ValueError(f"Invalid mode {mode!r}. Expected one of {MODES}.")
    if bugs_list is None:
        bugs_list = _load_bugs()

    proposed_text = _bug_text(proposed)

    dup = find_duplicate_bug(proposed_text, bugs_list)
    if dup:
        match_id = dup["match"].get("bugid", "?")
        return {
            "ok": False,
            "reason": (
                f"Semantic duplicate of existing bug {match_id} "
                f"(similarity {dup['score']:.2f}). Update existing rather than insert new."
            ),
            "action": f"UPDATE {match_id}",
            "match_id": match_id,
            "match_score": dup["score"],
        }

    req_id = (proposed.get("req_id") or "").strip()
    if not req_id:
        if mode == "unattended":
            return {
                "ok": True,
                "reason": "No req_id provided; Unattended Mode — insert as orphan (req_id=ORPHAN_REQ) per v4-033.",
                "action": "INSERT_AS_ORPHAN",
                "match_id": "",
            }
        if mode == "normal":
            return {
                "ok": False,
                "reason": "No req_id provided in Normal Mode; escalate for human assignment.",
                "action": "ESCALATE",
                "match_id": "",
            }
        # idiot
        return {
            "ok": False,
            "reason": "Idiot Mode refuses bug inserts without a human-assigned req_id.",
            "action": "REFUSE",
            "match_id": "",
        }

    return {
        "ok": True,
        "reason": "No duplicate; req_id present; insert authorized.",
        "action": "INSERT",
        "match_id": "",
    }


# ── Loaders ──────────────────────────────────────────────────────────────

def _load_bugs() -> list:
    """Read bugs.json and return its 'bug' array. Empty list if absent."""
    path = BRAIN_DIR / "bugs.json"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("bug", []) or []
    except Exception:
        return []


def _load_backlog() -> dict:
    """Read agile_backlog.json. Empty dict if absent/unreadable."""
    path = BRAIN_DIR / "agile_backlog.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _flatten_entries(backlog: dict, entry_type: str) -> list:
    """Return every backlog entry of the given type (epic/feature/story/req)
    flattened across the hierarchy."""
    result: list = []
    for epic in (backlog.get("epics", {}).get("epic", []) or []):
        if not isinstance(epic, dict):
            continue
        if entry_type == "epic":
            result.append(epic)
            continue
        for feature in (epic.get("features", {}).get("feature", []) or []):
            if not isinstance(feature, dict):
                continue
            if entry_type == "feature":
                result.append(feature)
                continue
            for story in (feature.get("stories", {}).get("story", []) or []):
                if not isinstance(story, dict):
                    continue
                if entry_type == "story":
                    result.append(story)
                    continue
                for req in (story.get("requirements", {}).get("requirement", []) or []):
                    if not isinstance(req, dict):
                        continue
                    if entry_type == "requirement":
                        result.append(req)
    return result
