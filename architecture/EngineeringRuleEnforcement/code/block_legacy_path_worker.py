"""Rule-064 enforcement worker — block tool actions targeting the
legacy `c:\\chatHealthy\\findCare` tree.

Wired to Claude Code's PreToolUse hook (matcher Bash|Write|Edit). Reads
the stdin JSON, inspects the tool input for any reference to the legacy
path, and emits a JSON response that either ALLOWS the call (silent) or
DENIES it with a reason.

This is the first enforcement that fires outside the git-hook chain.
It runs at tool-invocation time, before the tool executes, in the
Claude Code permission layer.
"""

from __future__ import annotations

import json
import re
import sys


# Patterns matched (case-insensitive) against tool inputs to detect
# legacy-tree targeting. ANY hit blocks the call.
_LEGACY_PATTERNS = [
    re.compile(r"c:[\\/]+chathealthy[\\/]+findcare", re.IGNORECASE),
    re.compile(r"c:[\\/]+chathealthy[\\/]+resources", re.IGNORECASE),
    # The legacy root itself; allow `c:\CHATHealthyLLC` (current project)
    # by requiring the next segment is NOT "LLC".
    re.compile(r"c:[\\/]+chathealthy(?![Ll])(?:[\\/]+|$)", re.IGNORECASE),
]


def _check(text: str) -> tuple[bool, str | None]:
    """Return (blocked, reason_if_blocked) for a single text payload."""
    if not text:
        return False, None
    for pat in _LEGACY_PATTERNS:
        m = pat.search(text)
        if m:
            return True, (
                f"Tool input references the LEGACY tree at {m.group(0)!r}. "
                f"Rule-064 blocks all reads/writes/commands targeting "
                f"c:\\chatHealthy\\findCare or c:\\chatHealthy\\Resources. "
                f"The current project lives at c:\\CHATHealthyLLC."
            )
    return False, None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        # Fail-open on parse error so we don't block legitimate work due
        # to a hook-data issue. Log to stderr for telemetry.
        print(f"block_legacy_path_worker: stdin parse error: {exc}", file=sys.stderr)
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    # Inspect the fields most likely to carry a legacy-path reference,
    # depending on which tool fired.
    candidates: list[str] = []
    if tool_name == "Bash":
        candidates.append(tool_input.get("command", ""))
    elif tool_name in ("Write", "Edit", "NotebookEdit"):
        candidates.append(tool_input.get("file_path", ""))
        candidates.append(tool_input.get("content", "") or "")
        candidates.append(tool_input.get("new_string", "") or "")
    else:
        # For other tools, scan all string values defensively.
        for v in tool_input.values():
            if isinstance(v, str):
                candidates.append(v)

    for text in candidates:
        blocked, reason = _check(text)
        if blocked:
            response = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
                "systemMessage": f"Rule-064 blocked: {reason}",
            }
            print(json.dumps(response))
            return 0

    # No legacy-path reference — allow the tool call (silent).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
