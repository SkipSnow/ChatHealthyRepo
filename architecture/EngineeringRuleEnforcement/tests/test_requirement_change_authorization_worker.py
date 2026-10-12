# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Behavioural tests for the Rule-003 provenance gate against
EPIC-008-F-002-S-013-REQ-B-002.

The gate's contract: a read of the backlog is never gated; a write to it by any
means other than invoking the sanctioned editor is a violation that is denied,
whatever the elevation. These tests exercise that classification and the deny
envelope directly -- no mocks of the backlog.
"""
import json
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[1] / "code"
sys.path.insert(0, str(CODE))

import agile_backlog_editor as ed                        # noqa: E402
import requirement_change_authorization_worker as gate   # noqa: E402

TARGET = str(ed.TARGET)


def _violation(tool, **tool_input):
    worker = gate.RequirementChangeAuthorization(
        {"tool_name": tool, "tool_input": tool_input})
    return worker.find_violation()


def test_reads_are_never_gated():
    assert _violation("Read", file_path=TARGET) is None
    assert _violation("Grep", pattern="x", path=TARGET) is None
    assert _violation("Bash", command=f'cat "{TARGET}"') is None
    assert _violation("PowerShell", command=f'Get-Content "{TARGET}"') is None


def test_direct_tool_edits_are_violations():
    assert _violation("Edit", file_path=TARGET) is not None
    assert _violation("Write", file_path=TARGET) is not None
    assert _violation("NotebookEdit", notebook_path=TARGET) is not None


def test_command_writes_are_violations():
    assert _violation(
        "Bash", command=f"python -c \"open(r'{TARGET}','w').write('x')\""
    ) is not None
    assert _violation("Bash", command=f'echo x > "{TARGET}"') is not None
    assert _violation("PowerShell",
                      command=f'Set-Content "{TARGET}" "x"') is not None
    assert _violation("PowerShell",
                      command=f'"x" | Out-File "{TARGET}"') is not None


def test_writes_to_other_files_are_allowed():
    assert _violation("Write", file_path=str(CODE / "foo.py")) is None


def test_invoking_the_sanctioned_editor_is_allowed():
    assert _violation(
        "Bash",
        command=f"python {CODE}/agile_backlog_editor.py --objects o.json"
    ) is None


def test_deny_emits_a_permission_decision_deny(capsys, monkeypatch):
    monkeypatch.setattr(gate.rule_violation_popup, "show", lambda *a, **k: None)
    worker = gate.RequirementChangeAuthorization(
        {"tool_name": "Edit", "tool_input": {"file_path": TARGET}})
    rc = worker.main()
    out = capsys.readouterr().out
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert rc == 0  # a refusal travels as JSON on stdout; the process exits 0
