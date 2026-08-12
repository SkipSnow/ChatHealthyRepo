"""Rule-002-ENF-001 and ENF-002 — boot on SessionStart and PostCompact.

ENF-002 had no test. The rule's third statement is explicit: SessionStart
and PostCompact each execute the boot duties, and any other hook is a no-op
that exits 0. A boot that silently did nothing on PostCompact would look
identical to a healthy one, because its only symptom is context Claude never
receives.

These run the real executable as the manager does — a subprocess fed a hook
payload on stdin — and assert on what it emits.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BOOT = (Path(__file__).resolve().parents[3]
        / "Code" / "Shared" / "ops" / "tools" / "chathealthy_devops_boot.py")


def _run(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(BOOT)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=120,
        cwd=str(BOOT.parents[4]),
    )


def test_the_boot_executable_exists():
    assert BOOT.is_file(), f"Rule-002 names {BOOT} and it must exist"


@pytest.mark.parametrize("event", ["SessionStart", "PostCompact"])
def test_boot_emits_additional_context(event):
    """Both hooks must produce hookSpecificOutput carrying additionalContext."""
    proc = _run({"hook_event_name": event})
    assert proc.returncode == 0, f"{event} exited {proc.returncode}: {proc.stderr[:300]}"
    payload = json.loads(proc.stdout)
    assert "hookSpecificOutput" in payload, \
        f"{event} produced no hookSpecificOutput: {proc.stdout[:200]}"
    hso = payload["hookSpecificOutput"]
    assert hso.get("hookEventName") == event, \
        f"the emitted event name must echo the hook; got {hso.get('hookEventName')}"
    assert hso.get("additionalContext", "").strip(), \
        f"{event} emitted empty additionalContext, so Claude receives nothing"


@pytest.mark.parametrize("event", ["Stop", "UserPromptSubmit", "PreToolUse", ""])
def test_other_hooks_are_a_silent_no_op(event):
    """Statement 3: any other hook_event_name is a no-op that exits 0."""
    proc = _run({"hook_event_name": event})
    assert proc.returncode == 0, f"{event!r} exited {proc.returncode}"
    assert "hookSpecificOutput" not in proc.stdout, \
        f"{event!r} must emit nothing; got {proc.stdout[:200]}"
