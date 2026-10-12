# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""EPIC-008-F-002-S-013-REQ-B-002 (Rule-003-ENF-001) -- the provenance gate.

The agile backlog is the firm's requirements, and the one sanctioned writer is
agile_backlog_editor.py, which gets the human authorization and, run elevated,
does the write. This gate guards MY command path:

  - A tool call that writes the backlog ANY way other than invoking the
    sanctioned program -- a direct Edit/Write/NotebookEdit, or a Bash/PowerShell
    command that writes it (shell redirect, tee, cp/mv, PowerShell
    Set-Content/Out-File, a python open-for-write, ...) -- is a VIOLATION: the
    "Claude broke the rules" notice pops and the call is denied. It is denied
    WHATEVER the elevation: being admin never buys a write outside the program.
  - A tool call that invokes the sanctioned program is allowed; the program does
    the authorization.
  - A read is never gated.

NO AUDIT TRAIL HERE. This gate only tells the operator when Claude cheats; the
legitimate change path (the editor) is what records to the audit. The hard block
is the file's ACL -- the backlog is admin-write-only, so a non-elevated direct
write also fails at the OS even if this gate ever missed it -- and the mechanism
is the git gate's: a browser notice served from a local HTTP server, and a
permissionDecision deny.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))
for _candidate in _HERE.parents:
    _lib = _candidate / "ChatHealthyLib" / "src"
    if _lib.is_dir():
        sys.path.insert(0, str(_lib))
        break

from chathealthy_lib import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

import rule_violation_popup  # noqa: E402

log = ChatHealthyLoggingService()


class RequirementChangeAuthorization:
    """Denies every write to the backlog that does not come from the editor."""

    COMPONENT = "requirement_change_authorization_worker"
    REQUIREMENT = "EPIC-008-F-002-S-013-REQ-B-002"
    ENFORCEMENT_ID = "Rule-003-ENF-001"

    TARGET_RELATIVE = "brain/machine_artifacts/content/agile_backlog.json"
    SANCTIONED_PROGRAM = "agile_backlog_editor.py"

    # A refusal travels as JSON on stdout and the process exits 0 -- the same
    # discipline as the git gate (exit 2 reads as a crashed worker and does not
    # block).
    EXIT_ALLOW = 0
    EXIT_AFTER_DENY = 0

    READ_TOOLS = frozenset({"Read", "Grep", "Glob"})
    WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})
    COMMAND_TOOLS = frozenset({"Bash", "PowerShell"})

    # Write intent across shell, PowerShell and python -- scanned only on a
    # command that already references the backlog, so these match writes TO it.
    WRITE_INDICATORS = (
        ">", ">>", "sed -i", "tee ", "cp ", "mv ", "dd ", "truncate", "rm ",
        "del ", "erase ", "move ", "copy ",
        "set-content", "out-file", "add-content", "clear-content",
        "copy-item", "move-item", "remove-item",
        "writealltext", "writealllines", "[io.file]::",
        ".write(", "writelines", "json.dump", "os.replace", "os.rename",
        "shutil.copy", "shutil.move",
        "'w'", '"w"', "'a'", '"a"', "'w+'", '"w+"', "'r+'", '"r+"', "'x'", '"x"',
    )

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.tool = payload.get("tool_name") or ""
        self.tool_input = payload.get("tool_input") or {}
        self.project_root = next(
            (p for p in _HERE.parents if (p / ".git").exists()),
            _HERE.parents[-1])
        self.target = (self.project_root / self.TARGET_RELATIVE).resolve()

    # -- the one entry point --------------------------------------------------
    def main(self) -> int:
        violation = self.find_violation()
        if violation is None:
            return self.EXIT_ALLOW
        self.notify(violation)
        return self.deny(violation)

    # -- is this a direct write to the backlog? -------------------------------
    def find_violation(self) -> dict | None:
        """The violation when this tool call writes the backlog any way other
        than invoking the sanctioned program, else None."""
        if self.tool in self.READ_TOOLS:
            return None
        if self.tool in self.WRITE_TOOLS:
            for field in ("file_path", "notebook_path", "path"):
                raw = str(self.tool_input.get(field) or "").strip()
                if raw and self._resolves_to_target(raw):
                    return {"how": self.tool}
            return None
        if self.tool in self.COMMAND_TOOLS:
            return self._command_violation()
        return None

    def _resolves_to_target(self, raw: str) -> bool:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.project_root / raw
        try:
            return candidate.resolve() == self.target
        except OSError:
            return False

    def _command_violation(self) -> dict | None:
        command = str(self.tool_input.get("command") or "")
        lowered = command.replace("\\", "/").lower()
        # Invoking the sanctioned program is the one allowed write path.
        if self.SANCTIONED_PROGRAM.lower() in lowered:
            return None
        names = (self.TARGET_RELATIVE.lower(), "agile_backlog.json",
                 str(self.target).replace("\\", "/").lower())
        if not any(name in lowered for name in names):
            return None
        if not any(token in lowered for token in self.WRITE_INDICATORS):
            return None
        return {"how": self.tool}

    # -- notify and deny ------------------------------------------------------
    def notify(self, violation: dict) -> None:
        rule_violation_popup.show(
            f"Claude tried to write the agile backlog -- the firm's requirements "
            f"-- via {violation['how']} instead of through "
            f"agile_backlog_editor.py. The write was denied. Only the sanctioned "
            f"editor, with your authorization, may change the backlog.",
            title="Claude broke the rules")

    def deny(self, violation: dict) -> int:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"{self.REQUIREMENT}: writing the agile backlog via "
                    f"{violation['how']} is a violation -- a change to the "
                    f"requirements must go through agile_backlog_editor.py."
                ),
            }
        }) + "\n")
        sys.stdout.flush()
        log.error("AUTHORIZATION violation: backlog write via %s denied",
                  violation["how"],
                  exc=ChatHealthyException(mode="security_violation",
                      component=self.COMPONENT,
                      message=f"direct backlog write via {violation['how']} "
                              "denied"),
                  if_not_debug_log=True)
        sys.stderr.write(
            f"DENIED by {self.REQUIREMENT}: Claude broke the rules -- a write to "
            f"the agile backlog via {violation['how']} must go through "
            f"agile_backlog_editor.py.\n")
        sys.stderr.flush()
        return self.EXIT_AFTER_DENY


def _payload() -> dict:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def main() -> int:
    return RequirementChangeAuthorization(_payload()).main()


if __name__ == "__main__":
    sys.exit(main())
