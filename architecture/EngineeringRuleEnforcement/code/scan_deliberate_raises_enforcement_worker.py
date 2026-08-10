"""scan_deliberate_raises_enforcement_worker.py — Rule-003 worker.

Enforces EPIC-008-F-002-S-009-REQ-B-006: deliberate raises in our
authored Python code MUST use ChatHealthyException with a mode
discriminator, not built-in exception types.

Mechanism: AST-based scan (no regex per Rule-008 statement 4). For every
staged .py file, parse and walk every `Raise` node. Reject if the raised
expression is a Call whose .func is a Name in the forbidden set
{ValueError, RuntimeError, AssertionError, KeyError, TypeError}.
Re-raises (bare `raise`) are always allowed. Any violations found are rejected.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from .enforcement_worker import (
        EnforcementWorker, ViolationRecord, PROJECT_ROOT,
        EXIT_OK, EXIT_VIOLATIONS_FOUND,
    )
except ImportError:
    from enforcement_worker import (  # noqa: E402
        EnforcementWorker, ViolationRecord, PROJECT_ROOT,
        EXIT_OK, EXIT_VIOLATIONS_FOUND,
    )


_FORBIDDEN: frozenset[str] = frozenset({
    "ValueError", "RuntimeError", "AssertionError",
    "KeyError", "TypeError",
})



def _ch_exception():
    """ChatHealthyException, resolved without assuming the library is on the
    path. Enforcement workers are spawned as bare scripts by the manager."""
    import sys as _sys, pathlib as _pl
    for _p in _pl.Path(__file__).resolve().parents:
        if (_p / ".git").exists():
            _lib = _p / "FrontEndApplicationLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            break
    from chathealthy_frontend_lib.exceptions import ChatHealthyException
    return ChatHealthyException

def _is_forbidden_raise(node: ast.Raise) -> str | None:
    """If node raises a Call on a forbidden built-in name, return that
    name; else None. Bare `raise` (no expression) returns None."""
    if node.exc is None:
        return None
    expr = node.exc
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
        if expr.func.id in _FORBIDDEN:
            return expr.func.id
    return None


def _count_forbidden_raises(source: str) -> list[tuple[int, str]]:
    """Return a list of (lineno, forbidden_name) for every forbidden
    Raise in the source. Unparseable source returns []."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise):
            name = _is_forbidden_raise(node)
            if name is not None:
                hits.append((node.lineno, name))
    return hits



class ScanDeliberateRaisesEnforcementWorker(EnforcementWorker):
    """Rule-003: deliberate raises must use ChatHealthyException."""

    SCOPE_DEFAULTS: dict[str, bool] = {
        "_scan_deliberate_raises": False,
    }
    SCOPE_DEFAULT: bool = False

    def __init__(self, enforcement_id: str) -> None:
        super().__init__(enforcement_id)
        self.files_scanned: int = 0
        self.violation_count: int = 0

    def _staged_files(self) -> list[str]:
        """The file array the Rule-065 driver handed down.

        This worker owns no git knowledge. What a commit answers for is one
        decision, and the driver makes it once for every subordinate.
        """
        return self.files

    def run(self) -> int:
        any_violations = False
        for file_path in self._staged_files():
            self.files_scanned += 1
            if not self.is_in_scope(file_path, "_scan_deliberate_raises"):
                continue
            for v in self._scan_deliberate_raises(file_path):
                self._emit_violation(v)
                self.violation_count += 1
                any_violations = True
        return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK

    def _scan_deliberate_raises(self, file_path: str) -> list[ViolationRecord]:
        """Reject any forbidden raises in staged file."""
        absolute_path = (PROJECT_ROOT / file_path).resolve()
        if not absolute_path.is_file():
            return []
        try:
            staged_text = absolute_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return []
        staged_hits = _count_forbidden_raises(staged_text)
        if not staged_hits:
            return []
        # Any violation in the staged file is rejected.
        violations: list[ViolationRecord] = []
        for lineno, name in staged_hits:
            violations.append(ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id="Rule-003",
                resource=f"{file_path}:{lineno}",
                message=(
                    f"deliberate raise of built-in {name!r}; "
                    f"EPIC-008-F-002-S-009-REQ-B-002 forbids this — use "
                    f"ChatHealthyException(mode=..., message=...) instead."
                ),
            ))
        return violations


if __name__ == "__main__":
    enforcement_id = sys.argv[1] if len(sys.argv) > 1 else "Rule-065-ENF-002"
    sys.exit(ScanDeliberateRaisesEnforcementWorker(enforcement_id).run())
