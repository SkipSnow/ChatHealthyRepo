"""ChatHealthy DevOps Boot - SessionStart and PostCompact handler.

Invoked by chathealthy_enforcement_manager.py per Rule-002. Reads the
hook payload from stdin, then writes context-resident artifacts to disk
so CLAUDE.md @-imports load them in full at session start / post-compact.

Hook additionalContext caps at ~10K chars, so it carries only a tiny
confirmation; the real payload lands via the @-import mechanism.
"""

from __future__ import annotations

import json

import os
import sys
import tempfile
from pathlib import Path

import sys as _ch_sys, pathlib as _ch_pl
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / ".git").exists():
        _ch_lib = _ch_d / "ChatHealthyLib" / "src"
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
# The chain materialises the application .env, which sets
# CH_LOG_DESTINATION=mongo and CH_LOG_DB=pipelineAdmin. Those are the
# deployed application's facts, not this tool's: devops tooling runs on
# a workstation and its log is the operator's terminal. Inheriting them
# made a build depend on a Mongo write it has no grant for.
import os as _ch_os
_ch_os.environ["CH_LOG_DESTINATION"] = "stderr"
from chathealthy_lib.logging_service import ChatHealthyLoggingService

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[4] / "Code" / ".env")
except ImportError:
    pass

_log = ChatHealthyLoggingService()


PROJECT_ROOT = Path(__file__).resolve().parents[4]
BRAIN_DIR = PROJECT_ROOT / "brain" / "machine_artifacts" / "content"
TEST_OUTPUT_DIR = PROJECT_ROOT / "_oneshots" / "test_output"

CONVERSATION_LOAD_COUNT = 170
MONGO_DB = "ClaudeCodeUtterances"
MONGO_COLLECTION = "utterances"

ERROR_LOG_PATH = Path(tempfile.gettempdir()) / "chathealthy_consumer_errors.log"
ERROR_LOG_SEMAPHORE = Path(tempfile.gettempdir()) / "chathealthy_last_boot_error_line.txt"


def _devops_connection():
    """The DevOps identity, by certificate. Rule-004: no MongoClient here."""
    import sys as _sys, pathlib as _pl
    for _p in _pl.Path(__file__).resolve().parents:
        if (_p / ".git").exists():
            _lib = _p / "ChatHealthyLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            # Hook subprocesses inherit almost nothing, so the vault
            # coordinates have to come from the file rather than the
            # ambient environment.
            from dotenv import load_dotenv as _ld
            _ld(_p / ".env", override=False)
            break
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    return ChatHealthyMongoUtilities().getConnection("DevOpsUser", "admin")


def load_recent_utterances(n: int = CONVERSATION_LOAD_COUNT) -> list[dict]:
    # No credential check here. Authentication is the certificate the
    # DevOps identity fetches from the vault; gating on the presence of an
    # unrelated SCRAM connection string meant the boot silently skipped the
    # conversation load whenever that variable was absent from the process.
    try:
        client = _devops_connection()
        coll = client[MONGO_DB][MONGO_COLLECTION]
        cursor = (
            coll.find({}, {"role": 1, "content": 1, "_id": 0})
                .sort("timestamp", -1)
                .limit(n)
        )
        utterances = list(cursor)
        client.close()
    except Exception as e:
        _log.warning("Mongo read failed: %s", e)
        return []
    return utterances


def write_utterances_file(utterances: list[dict]) -> Path:
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = TEST_OUTPUT_DIR / "recent_utterances.json"
    out.write_text(
        json.dumps(
            [{"actor": u.get("role") or u.get("actor"), "content": u["content"]}
             for u in utterances],
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return out


def build_story_tree(backlog: dict) -> dict:
    epics_out = []
    epics_blob = backlog.get("epics", {})
    epic_list = epics_blob.get("epic", []) if isinstance(epics_blob, dict) else epics_blob
    for e in epic_list:
        if not isinstance(e, dict):
            continue
        feats_out = []
        feats_blob = e.get("features", {})
        feat_list = feats_blob.get("feature", []) if isinstance(feats_blob, dict) else feats_blob
        for f in feat_list:
            if not isinstance(f, dict):
                continue
            stories_out = []
            stories_blob = f.get("stories", {})
            story_list = stories_blob.get("story", []) if isinstance(stories_blob, dict) else stories_blob
            for s in story_list:
                if not isinstance(s, dict):
                    continue
                reqs_blob = s.get("requirements", {})
                req_list = reqs_blob.get("requirement", []) if isinstance(reqs_blob, dict) else reqs_blob
                stories_out.append({
                    "story_id": s.get("story_id"),
                    "title": s.get("name"),
                    "description": s.get("description"),
                    "parent_feature": f.get("feature_id"),
                    "sprint": s.get("sprint"),
                    "size": s.get("size"),
                    "status": s.get("status"),
                    "accepted_by": s.get("accepted_by"),
                    "orphan": s.get("orphan"),
                    "approval": s.get("approval"),
                    "requirement_count": len(req_list) if isinstance(req_list, list) else 0,
                })
            feats_out.append({
                "feature_id": f.get("feature_id"),
                "name": f.get("name"),
                "description": f.get("description"),
                "stories": stories_out,
            })
        epics_out.append({
            "epic_id": e.get("epic_id"),
            "name": e.get("name"),
            "description": e.get("description"),
            "features": feats_out,
        })
    return {"epics": epics_out}


def write_backlog_stories_file() -> Path:
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = TEST_OUTPUT_DIR / "backlog_stories.json"
    backlog = json.loads((BRAIN_DIR / "agile_backlog.json").read_text(encoding="utf-8"))
    tree = build_story_tree(backlog)
    out.write_text(json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def surface_consumer_errors() -> str:
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


def run_boot(event_name: str) -> None:
    utterances = load_recent_utterances()
    utt_path = write_utterances_file(utterances)
    write_backlog_stories_file()
    rel = utt_path.relative_to(PROJECT_ROOT).as_posix()
    instruction = (
        f"FIRST ACTION: Read('{rel}') to load the {len(utterances)} most "
        f"recent conversation utterances into context, then Bash('rm \"{rel}\"') "
        f"to delete the transport file. Do this before any other tool call."
    )
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": "\n\n".join((instruction, surface_consumer_errors())),
        }
    }, sys.stdout)


def main() -> int:
    payload = json.load(sys.stdin)
    event = payload.get("hook_event_name", "")
    if event in ("SessionStart", "PostCompact"):
        run_boot(event)
    return 0


if __name__ == "__main__":
    sys.exit(main())
