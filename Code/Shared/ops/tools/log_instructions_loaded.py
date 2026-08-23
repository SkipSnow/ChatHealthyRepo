#!/usr/bin/env python3
# EPIC-008-F-003-S-001/016/017 — InstructionsLoaded hook.
#
# For every instruction file the harness loads, append one line to
# _oneshots/test_output/instructions_loaded.log. For the three context-resident
# brain JSONs (bugs.json, engineering_rules.json, agile_backlog.json),
# run the 4-step verification procedure from REQ-017:
#   (1) validate JSON                  — fail reason: invalid_json
#   (2) compute sha256 on disk         — fail reason: hash_unreadable
#   (3) confirm harness load           — implicit: this hook firing IS step 3
#   (4) compare hash vs precompact     — fail reason: hash_mismatch
#
# Success emits one "LOADED" record per brain JSON. Failure emits one
# failure record naming exactly one of the four reasons. The
# "not_loaded" verdict is emitted by a post-session reader, not here
# (absence of a record for a brain file is how not_loaded is detected).
#
# Log-only, non-blocking, always exits 0.
import hashlib
import json
import os
import sys
import time
from pathlib import Path
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR", "."))
LOG = PROJECT / "_oneshots/test_output" / "instructions_loaded.log"
PRECOMPACT = PROJECT / "_oneshots/test_output" / "brain_precompact_hashes.json"

BRAIN_JSONS = {"bugs.json", "engineering_rules.json", "agile_backlog.json"}


def _write(record: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


try:
    payload = json.load(sys.stdin)
except Exception as e:
    _write({"ts": time.time(), "parse_error": str(e)})
    raise ChatHealthyException(
    mode="aborted",
    component="log_instructions_loaded",
    message=0,
        exception=e)

file_path = payload.get("file_path", "")
size = None
try:
    if file_path and os.path.isfile(file_path):
        size = os.path.getsize(file_path)
except Exception:
    pass

base = {
    "ts": time.time(),
    "session_id": payload.get("session_id", ""),
    "file_path": file_path,
    "memory_type": payload.get("memory_type", ""),
    "load_reason": payload.get("load_reason", ""),
    "parent_file_path": payload.get("parent_file_path", ""),
    "trigger_file_path": payload.get("trigger_file_path", ""),
    "size_bytes": size,
}

fname = os.path.basename(file_path) if file_path else ""
if fname not in BRAIN_JSONS:
    _write(base)
    raise ChatHealthyException(
        mode="aborted",
        component="log_instructions_loaded",
        message=0)

# ── REQ-017: 4-step procedure for brain JSONs ─────────────────────────────
# Step 3 is implicit: this hook firing means the harness loaded the file.
verdict = {"verdict": None, "reason": None, "sha256": None}

# Step 1: validate JSON
try:
    with open(file_path, "rb") as f:
        data = f.read()
    try:
        json.loads(data)
    except Exception as je:
        verdict = {"verdict": "FAILED", "reason": "invalid_json", "detail": str(je)[:200]}
        _write({**base, **verdict})
        raise ChatHealthyException(
            mode="aborted",
            component="log_instructions_loaded",
            message=f"instructions file is not valid JSON: {str(je)[:200]}",
            exit_code=0,
            exception=je)
except Exception as ie:
    verdict = {"verdict": "FAILED", "reason": "hash_unreadable", "detail": str(ie)[:200]}
    _write({**base, **verdict})
    raise ChatHealthyException(
        mode="aborted",
        component="log_instructions_loaded",
        message=0,
        exception=ie)

# Step 2: hash on disk
try:
    sha = hashlib.sha256(data).hexdigest()
    verdict["sha256"] = sha
except Exception as he:
    verdict = {"verdict": "FAILED", "reason": "hash_unreadable", "detail": str(he)[:200]}
    _write({**base, **verdict})
    raise ChatHealthyException(
        mode="aborted",
        component="log_instructions_loaded",
        message=0,
        exception=he)

# Step 4: compare to precompact hash
try:
    pre = json.loads(PRECOMPACT.read_text(encoding="utf-8"))
except Exception:
    # No precompact file — can't verify step 4. Emit LOADED with a caveat;
    # this is the first-session-after-bootstrap case.
    verdict["verdict"] = "LOADED"
    verdict["reason"] = None
    verdict["precompact_present"] = False
    _write({**base, **verdict})
    raise ChatHealthyException(
        mode="aborted",
        component="log_instructions_loaded",
        message=0)

normalized = str(Path(file_path)).replace("\\", "/")
pre_entry = pre.get(normalized) or pre.get(file_path)
if not pre_entry:
    # file not in precompact map — still emit LOADED with caveat
    verdict["verdict"] = "LOADED"
    verdict["reason"] = None
    verdict["precompact_present"] = False
    _write({**base, **verdict})
    raise ChatHealthyException(
        mode="aborted",
        component="log_instructions_loaded",
        message=0)

pre_sha = pre_entry.get("sha256")
if pre_sha and pre_sha != sha:
    verdict = {"verdict": "FAILED", "reason": "hash_mismatch",
               "sha256_precompact": pre_sha, "sha256_onload": sha}
    _write({**base, **verdict})
    raise ChatHealthyException(
        mode="aborted",
        component="log_instructions_loaded",
        message=0)

verdict["verdict"] = "LOADED"
verdict["reason"] = None
verdict["precompact_present"] = True
_write({**base, **verdict})
raise ChatHealthyException(
            mode="aborted",
            component="log_instructions_loaded",
            message=0)
