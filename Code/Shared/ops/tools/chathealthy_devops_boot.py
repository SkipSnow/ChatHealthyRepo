"""ChatHealthy DevOps Boot — SessionStart and PostCompact handler.

Invoked by chathealthy_enforcement_manager.py per Rule-002. Reads the
hook payload from stdin to learn which event fired, then runs the boot
duties.

Both SessionStart and PostCompact trigger the same duties so Claude
resumes with fresh context whether the disruption was a session boundary
or a mid-session context compaction:
  1. Load 170 most recent utterances from MongoDB, newest-to-oldest
  2. Load agile_backlog taxonomy (epic + feature descriptions, story names)

Any other hook_event_name is a no-op exit 0.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[4] / "Code" / ".env")
except ImportError:
    pass

_log = logging.getLogger("devops_boot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

PROJECT_ROOT = Path(__file__).resolve().parents[4]
BRAIN_DIR = PROJECT_ROOT / "brain" / "machine_artifacts" / "content"

CONVERSATION_LOAD_COUNT = 170
MONGO_DB = "ClaudeCodeLog"
MONGO_COLLECTION = "conversation_log_archive"

ERROR_LOG_PATH = Path(tempfile.gettempdir()) / "chathealthy_consumer_errors.log"
ERROR_LOG_SEMAPHORE = Path(tempfile.gettempdir()) / "chathealthy_last_boot_error_line.txt"


# ── Statement 1: load recent utterances from Mongo ───────────────────────────


def load_recent_utterances(n: int = CONVERSATION_LOAD_COUNT) -> list[dict]:
    """Load N most recent utterances from Mongo, trimmed to {actor, content},
    returned newest-first. Returns [] on any failure (fail-open per Rule-002).
    """
    conn_str = os.environ.get("MONGO_FRONTEND_connectionString", "")
    if not conn_str:
        _log.warning("MONGO_FRONTEND_connectionString not set — skipping conversation load")
        return []
    try:
        from pymongo import MongoClient
        client = MongoClient(conn_str, serverSelectionTimeoutMS=5000)
        coll = client[MONGO_DB][MONGO_COLLECTION]
        cursor = (
            coll.find({}, {"actor": 1, "content": 1, "_id": 0})
                .sort("timestamp_utc", -1)
                .limit(n)
        )
        utterances = list(cursor)
        client.close()
    except Exception as e:
        _log.warning("Mongo read failed: %s", e)
        return []
    return utterances


def format_utterances(utterances: list[dict]) -> str:
    if not utterances:
        return ""
    lines = [f"## Recent conversation context ({len(utterances)} utterances, newest first)\n"]
    for u in utterances:
        lines.append(json.dumps({
            "actor": u.get("actor", "Unknown"),
            "content": u.get("content", ""),
        }, ensure_ascii=False))
    return "\n".join(lines)


# ── Statement 2: agile_backlog taxonomy ──────────────────────────────────────


def load_taxonomy() -> str:
    """Walk agile_backlog.json. Emit epic id+name+description, feature
    id+name+description, story id+name. Excludes story descriptions,
    requirements, tests."""
    backlog_path = BRAIN_DIR / "agile_backlog.json"
    data = json.loads(backlog_path.read_text(encoding="utf-8"))

    lines = ["## Business taxonomy (agile_backlog.json)\n"]
    epics_blob = data.get("epics", {})
    epic_list = epics_blob.get("epic", []) if isinstance(epics_blob, dict) else epics_blob

    for e in epic_list:
        if not isinstance(e, dict):
            continue
        eid = e.get("epic_id", "?")
        ename = e.get("name", "")
        edesc = e.get("description", "")
        lines.append(f"### {eid} — {ename}")
        if edesc:
            lines.append(edesc)

        feats_blob = e.get("features", {})
        feat_list = feats_blob.get("feature", []) if isinstance(feats_blob, dict) else feats_blob
        for f in feat_list:
            if not isinstance(f, dict):
                continue
            fid = f.get("feature_id", "?")
            fname = f.get("name", "")
            fdesc = f.get("description", "")
            lines.append(f"\n#### {fid} — {fname}")
            if fdesc:
                lines.append(fdesc)

            stories_blob = f.get("stories", {})
            story_list = stories_blob.get("story", []) if isinstance(stories_blob, dict) else stories_blob
            if story_list:
                lines.append("\n**Stories:**")
                for s in story_list:
                    if not isinstance(s, dict):
                        continue
                    sid = s.get("story_id", "?")
                    sname = s.get("name", "")
                    lines.append(f"- {sid} — {sname}")
        lines.append("")

    return "\n".join(lines)


# ── Statement 5: surface consumer error log pointer ──────────────────────────


def surface_consumer_errors() -> str:
    """Compare line count of consumer error log to semaphore. Return a one-line
    pointer if new entries exist; empty string otherwise. Update the semaphore
    to the current line count on every call."""
    if not ERROR_LOG_PATH.exists():
        return ""
    with open(ERROR_LOG_PATH, "r", encoding="utf-8") as f:
        current_lines = sum(1 for _ in f)
    semaphore = 0
    if ERROR_LOG_SEMAPHORE.exists():
        try:
            semaphore = int(ERROR_LOG_SEMAPHORE.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            semaphore = 0
    if current_lines <= semaphore:
        return ""
    start_line = semaphore + 1
    new_count = current_lines - semaphore
    ERROR_LOG_SEMAPHORE.write_text(str(current_lines), encoding="utf-8")
    return (
        f"CONSUMER ERRORS: {new_count} new entries in {ERROR_LOG_PATH} "
        f"(lines {start_line}-{current_lines})."
    )


# ── Lifecycle entry ──────────────────────────────────────────────────────────


def run_boot(event_name: str) -> None:
    """Execute boot duties per Rule-002 statements 1, 2, and 5.

    Invoked on SessionStart and PostCompact. The output's hookEventName
    matches whichever event fired so Claude Code routes the additionalContext
    back to that hook's lifecycle phase.
    """
    parts: list[str] = []

    utterances = load_recent_utterances()
    formatted = format_utterances(utterances)
    if formatted:
        parts.append(formatted)

    taxonomy = load_taxonomy()
    if taxonomy:
        parts.append(taxonomy)

    error_pointer = surface_consumer_errors()
    if error_pointer:
        parts.append(error_pointer)

    output = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": "\n\n".join(parts),
        }
    }
    json.dump(output, sys.stdout)


# ── CLI entry point ──────────────────────────────────────────────────────────


def main() -> int:
    payload = json.load(sys.stdin)
    event = payload.get("hook_event_name", "")

    if event in ("SessionStart", "PostCompact"):
        run_boot(event)

    return 0


if __name__ == "__main__":
    sys.exit(main())
