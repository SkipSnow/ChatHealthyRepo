# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# brain_write_validator — BRAIN-RUNTIME-REQ-002, REQ-003, REQ-004.
#
# Validates proposed insertions into bugs.json and agile_backlog.json:
#   - Finds semantic duplicates (text-similarity) in existing records.
#   - Checks logical-parent presence (req_id for bugs; parent hierarchy for backlog).
#   - Applies mode-aware orphan fallback (Unattended → orphan; Normal/Idiot → escalate).
#
# Also exposes cache-freshness checking for REQ-004 (detect mid-session edits).
#
# No GPT dependency — uses difflib.SequenceMatcher for similarity. GPT-based
# semantic matching is a later enhancement (BUG-GOV-006 tracks structured I/O
# upgrade of guard GPT calls).

from __future__ import annotations

import difflib
import json
from pathlib import Path

BRAIN_DIR = Path(__file__).resolve().parents[4] / "brain" / "machine_artifacts" / "content"

# Similarity threshold for semantic-duplicate detection.
# 0.60 catches obvious rewordings without over-flagging unrelated reqs.
SIMILARITY_THRESHOLD = 0.60

# Three brain artifacts governed by BRAIN-RUNTIME requirements.
RUNTIME_BRAIN_FILES = ("bugs.json", "engineering_rules.json", "agile_backlog.json")

# Mode vocabulary (CV-003).
MODES = ("unattended", "normal", "idiot")


# ── Similarity ────────────────────────────────────────────────────────────────

def _text_similarity(a: str, b: str) -> float:
    """Return SequenceMatcher ratio in [0, 1]. Empty inputs → 0."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def find_duplicate_bug(proposed_text: str, bugs_list: list,
                       threshold: float = SIMILARITY_THRESHOLD) -> dict | None:
    """Return {'match': bug, 'score': float} for the closest existing bug
    whose similarity to proposed_text meets or exceeds threshold, else None."""
    best = None
    best_score = 0.0
    for bug in bugs_list or []:
        existing_text = " ".join(filter(None, [
            bug.get("title") or "",
            bug.get("description") or "",
            bug.get("reproduction") or "",
        ]))
        score = _text_similarity(proposed_text, existing_text)
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
    best = None
    best_score = 0.0
    for req in reqs_list or []:
        existing_text = req.get("requirement") or ""
        score = _text_similarity(proposed_text, existing_text)
        if score > best_score:
            best_score = score
            best = req
    if best is not None and best_score >= threshold:
        return {"match": best, "score": best_score}
    return None


# ── Insert validation (BRAIN-RUNTIME-REQ-002, REQ-003) ────────────────────────

def validate_bug_insert(proposed: dict, mode: str,
                        bugs_list: list | None = None) -> dict:
    """Validate a proposed bug record per BRAIN-RUNTIME-REQ-002.

    Returns dict: {
        'ok': bool — safe to insert?
        'reason': str — explanation,
        'action': str — one of 'INSERT', 'UPDATE <id>', 'ESCALATE',
                        'INSERT_AS_ORPHAN',
        'match_id': str — existing bug id if duplicate found,
    }
    """
    if mode not in MODES:
        raise ValueError(f"Invalid mode {mode!r}. Expected one of {MODES}.")
    if bugs_list is None:
        bugs_list = _load_bugs()

    proposed_text = " ".join(filter(None, [
        proposed.get("title") or "",
        proposed.get("description") or "",
        proposed.get("reproduction") or "",
    ]))

    dup = find_duplicate_bug(proposed_text, bugs_list)
    if dup:
        return {
            "ok": False,
            "reason": (
                f"Semantic duplicate of existing bug {dup['match'].get('bugid', '?')} "
                f"(similarity {dup['score']:.2f}). Update existing rather than insert new."
            ),
            "action": f"UPDATE {dup['match'].get('bugid', '?')}",
            "match_id": dup["match"].get("bugid", ""),
            "match_score": dup["score"],
        }

    req_id = proposed.get("req_id") or ""
    if not req_id:
        if mode == "unattended":
            return {
                "ok": True,
                "reason": "No req_id provided; Unattended Mode → insert with req_id=ORPHAN_BUG per v4-033.",
                "action": "INSERT_AS_ORPHAN",
                "match_id": "",
            }
        return {
            "ok": False,
            "reason": (
                f"No req_id provided. In {mode.capitalize()} Mode, Claude MUST escalate "
                "to Boss before inserting (v4-033 / BRAIN-RUNTIME-REQ-002)."
            ),
            "action": "ESCALATE",
            "match_id": "",
        }

    return {
        "ok": True,
        "reason": "Validated — novel, parent req_id provided.",
        "action": "INSERT",
        "match_id": "",
    }


def validate_backlog_insert(proposed: dict, entry_type: str, parent_id: str,
                            mode: str, backlog: dict | None = None) -> dict:
    """Validate a proposed backlog entry per BRAIN-RUNTIME-REQ-003.

    entry_type: 'requirement' | 'story' | 'feature' | 'epic'
    parent_id: id of the intended parent, or empty if none found.
    """
    if mode not in MODES:
        raise ValueError(f"Invalid mode {mode!r}. Expected one of {MODES}.")
    if entry_type not in ("requirement", "story", "feature", "epic"):
        raise ValueError(f"Invalid entry_type {entry_type!r}.")
    if backlog is None:
        backlog = _load_backlog()

    existing = _flatten_entries(backlog, entry_type)

    if entry_type == "requirement":
        proposed_text = proposed.get("requirement") or ""
        dup = find_duplicate_req(proposed_text, existing)
    else:
        proposed_text = " ".join(filter(None, [
            proposed.get("title") or proposed.get("name") or "",
            proposed.get("description") or "",
        ]))
        dup = None
        best_score = 0.0
        for e in existing:
            existing_text = " ".join(filter(None, [
                e.get("title") or e.get("name") or "",
                e.get("description") or "",
            ]))
            score = _text_similarity(proposed_text, existing_text)
            if score > best_score:
                best_score = score
                if score >= SIMILARITY_THRESHOLD:
                    dup = {"match": e, "score": score}

    if dup:
        match_id = (dup["match"].get("req_id") or dup["match"].get("story_id")
                    or dup["match"].get("feature_id") or dup["match"].get("epic_id") or "?")
        return {
            "ok": False,
            "reason": (
                f"Semantic duplicate of existing {entry_type} {match_id} "
                f"(similarity {dup['score']:.2f}). Update existing rather than insert new."
            ),
            "action": f"UPDATE {match_id}",
            "match_id": match_id,
            "match_score": dup["score"],
        }

    if not parent_id:
        if mode == "unattended":
            return {
                "ok": True,
                "reason": "No logical parent; Unattended Mode → insert with orphan=true under nearest ancestor.",
                "action": "INSERT_AS_ORPHAN",
                "match_id": "",
            }
        return {
            "ok": False,
            "reason": (
                f"No logical parent. In {mode.capitalize()} Mode, Claude MUST escalate "
                "to Boss before inserting (BRAIN-RUNTIME-REQ-003)."
            ),
            "action": "ESCALATE",
            "match_id": "",
        }

    return {
        "ok": True,
        "reason": "Validated — novel, parent provided.",
        "action": "INSERT",
        "match_id": "",
    }


# ── Cache-freshness (BRAIN-RUNTIME-REQ-004) ───────────────────────────────────

def snapshot_brain_mtimes(brain_dir: Path | None = None) -> dict[str, float]:
    """Return current mtime of each governed brain file. Missing files → 0.0."""
    d = brain_dir or BRAIN_DIR
    result: dict[str, float] = {}
    for fname in RUNTIME_BRAIN_FILES:
        path = d / fname
        result[fname] = path.stat().st_mtime if path.exists() else 0.0
    return result


def detect_stale_brain_files(
    last_known: dict[str, float], brain_dir: Path | None = None,
) -> list[str]:
    """Compare current mtimes to last_known. Return list of files whose
    on-disk mtime is newer than last_known (i.e. edited since last snapshot).
    These are the files Claude's in-context copy is stale for and MUST be
    refreshed per BRAIN-RUNTIME-REQ-004 before any further action."""
    current = snapshot_brain_mtimes(brain_dir)
    stale: list[str] = []
    for fname, cur_mtime in current.items():
        prev = last_known.get(fname, 0.0)
        if cur_mtime > prev:
            stale.append(fname)
    return stale


# ── Internals ─────────────────────────────────────────────────────────────────

def _load_bugs() -> list:
    path = BRAIN_DIR / "bugs.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("bug", []) or []


def _load_backlog() -> dict:
    path = BRAIN_DIR / "agile_backlog.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _flatten_entries(backlog: dict, entry_type: str) -> list:
    """Return all backlog entries of the given type, flat."""
    result: list = []
    for epic in (backlog.get("epics", {}).get("epic", []) or []):
        if not isinstance(epic, dict):
            continue
        if entry_type == "epic":
            result.append(epic)
            continue
        for feat in (epic.get("features", {}).get("feature", []) or []):
            if not isinstance(feat, dict):
                continue
            if entry_type == "feature":
                result.append(feat)
                continue
            for story in (feat.get("stories", {}).get("story", []) or []):
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
