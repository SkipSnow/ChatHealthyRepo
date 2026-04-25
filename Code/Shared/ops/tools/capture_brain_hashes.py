#!/usr/bin/env python3
# EPIC-008-F-003-S-001-REQ-T-009 step 2: at SessionStart, hash each
# context-resident brain JSON on disk and retain the hash. This is
# the pre-load baseline the InstructionsLoaded logger compares
# against to detect hash_mismatch.
#
# Output: test_output/brain_precompact_hashes.json
#   { "session_id": "...", "timestamp": epoch,
#     "<full_path>": {"sha256": "...", "size_bytes": N, "valid_json": bool,
#                      "error": "..."}}
#
# Never raises. Always exits 0 so the SessionStart hook chain is not disrupted.
import hashlib
import json
import os
import sys
import time
from pathlib import Path

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR", "."))
OUT = PROJECT / "test_output" / "brain_precompact_hashes.json"
BRAIN = PROJECT / "brain" / "machine_artifacts" / "content"

BRAIN_JSONS = ("bugs.json", "engineering_rules.json", "agile_backlog.json")

try:
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
except Exception:
    payload = {}

session_id = payload.get("session_id", "")

record = {
    "session_id": session_id,
    "timestamp": time.time(),
}

for name in BRAIN_JSONS:
    path = BRAIN / name
    entry = {"sha256": None, "size_bytes": None, "valid_json": None, "error": None}
    try:
        data = path.read_bytes()
        entry["size_bytes"] = len(data)
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        try:
            json.loads(data)
            entry["valid_json"] = True
        except Exception as je:
            entry["valid_json"] = False
            entry["error"] = f"invalid_json: {je}"
    except Exception as ie:
        entry["error"] = f"hash_unreadable: {ie}"
    record[str(path).replace("\\", "/")] = entry

try:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(record, indent=2), encoding="utf-8")
except Exception:
    pass

sys.exit(0)
