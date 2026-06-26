"""scan_deliberate_raises_enforcement_worker.py — Rule-003 worker.

Enforces EPIC-008-F-002-S-009-REQ-B-006: deliberate raises in our
authored Python code MUST use ChatHealthyException with a mode
discriminator, not built-in exception types.

Mechanism: AST-based scan (no regex per Rule-008 statement 4). For every
staged .py file, parse and walk every `Raise` node. Reject if the raised
expression is a Call whose .func is a Name in the forbidden set
{ValueError, RuntimeError, AssertionError, KeyError, TypeError}.
Re-raises (bare `raise`) are always allowed.

Historical-violations carve-out per REQ-B-006: count violations in the
staged file vs the HEAD version of the same file; reject only if the
staged version has MORE forbidden raises than HEAD. Fixing or moving
existing violations is allowed; introducing NEW ones is not.
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


def _head_source(repo_relative_path: str) -> str:
    """Return the HEAD version of the file as a string. Empty string if
    the file is new (no HEAD entry) or any git error."""
    completed = subprocess.run(
        ["git", "show", f"HEAD:{repo_relative_path}"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout


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
        """Repo-relative .py paths staged for this pre-commit. Supports
        override via SCAN_FILES_ENFORCEMENT_TARGETS for tests."""
        import os
        override = os.environ.get("SCAN_FILES_ENFORCEMENT_TARGETS")
        if override is not None:
            return [p for p in override.split(os.pathsep)
                    if p and p.endswith(".py")]
        if self.hook != "pre-commit":
            return []
        completed = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return []
        return [
            line for line in completed.stdout.splitlines()
            if line.strip() and line.endswith(".py")
        ]

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
        """Reject only NEW forbidden raises: staged-count - head-count > 0
        triggers a violation. The violation enumerates each forbidden
        raise present in the staged version so the operator sees what
        to convert; the gate fires only when the staged version has
        strictly more than the HEAD version had."""
        absolute_path = (PROJECT_ROOT / file_path).resolve()
        if not absolute_path.is_file():
            return []
        try:
            staged_text = absolute_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return []
        staged_hits = _count_forbidden_raises(staged_text)
        head_text = _head_source(file_path)
        head_hits = _count_forbidden_raises(head_text) if head_text else []
        if len(staged_hits) <= len(head_hits):
            return []
        # Net-new violation introduced in this commit. List every
        # forbidden raise in the staged version so the operator sees the
        # full state to remediate.
        violations: list[ViolationRecord] = []
        for lineno, name in staged_hits:
            violations.append(ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id="Rule-003",
                resource=f"{file_path}:{lineno}",
                message=(
                    f"deliberate raise of built-in {name!r}; "
                    f"EPIC-008-F-002-S-009-REQ-B-002 forbids this — use "
                    f"ChatHealthyException(mode=..., message=...) instead. "
                    f"(staged count={len(staged_hits)}, HEAD count="
                    f"{len(head_hits)})"
                ),
            ))
        return violations


if __name__ == "__main__":
    enforcement_id = sys.argv[1] if len(sys.argv) > 1 else "Rule-003-ENF-001"
    sys.exit(ScanDeliberateRaisesEnforcementWorker(enforcement_id).run())
