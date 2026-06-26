"""scan_mongo_client_direct_access_worker.py — Rule-004 worker.

Enforces EPIC-008-F-002-S-010-REQ-B-007: direct MongoClient(...)
instantiation is forbidden in any ChatHealthy.ai-authored Python file
except within FrontEndApplicationLib/src/chathealthy_frontend_lib/
mongo_utilities.py itself.

Mechanism: AST-based scan (no regex per Rule-008 statement 4). For every
in-scope staged .py file, parse and walk every Call node; reject when
the call target resolves to a name 'MongoClient' (either bare
`MongoClient(...)` or attribute access like `pymongo.MongoClient(...)`).
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

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


_FORBIDDEN_NAME = "MongoClient"


def _is_mongo_client_call(node: ast.Call) -> bool:
    """True when the Call target is the MongoClient name (bare or
    qualified). Bare: `MongoClient(...)`. Qualified: `pymongo.MongoClient(...)`."""
    func = node.func
    if isinstance(func, ast.Name) and func.id == _FORBIDDEN_NAME:
        return True
    if isinstance(func, ast.Attribute) and func.attr == _FORBIDDEN_NAME:
        return True
    return False


def _find_mongo_client_calls(source: str) -> list[int]:
    """Return line numbers of every MongoClient(...) call in source.
    Unparseable source returns []."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_mongo_client_call(node):
            hits.append(node.lineno)
    return hits


class ScanMongoClientDirectAccessEnforcementWorker(EnforcementWorker):
    """Rule-004: direct MongoClient(...) forbidden outside the utility."""

    SCOPE_DEFAULTS: dict[str, bool] = {
        "_scan_mongo_client_direct_access": False,
    }
    SCOPE_DEFAULT: bool = False

    def __init__(self, enforcement_id: str) -> None:
        super().__init__(enforcement_id)
        self.files_scanned: int = 0
        self.violation_count: int = 0

    def _staged_files(self) -> list[str]:
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
            if not self.is_in_scope(file_path, "_scan_mongo_client_direct_access"):
                continue
            for v in self._scan_mongo_client_direct_access(file_path):
                self._emit_violation(v)
                self.violation_count += 1
                any_violations = True
        return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK

    def _scan_mongo_client_direct_access(self, file_path: str) -> list[ViolationRecord]:
        absolute_path = (PROJECT_ROOT / file_path).resolve()
        if not absolute_path.is_file():
            return []
        try:
            source = absolute_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return []
        violations: list[ViolationRecord] = []
        for lineno in _find_mongo_client_calls(source):
            violations.append(ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id="Rule-004",
                resource=f"{file_path}:{lineno}",
                message=(
                    "direct MongoClient(...) instantiation is forbidden "
                    "outside FrontEndApplicationLib/src/chathealthy_frontend_lib/"
                    "mongo_utilities.py per EPIC-008-F-002-S-010-REQ-B-007. "
                    "Use ChatHealthyMongoUtilities().getConnection() instead."
                ),
            ))
        return violations


if __name__ == "__main__":
    enforcement_id = sys.argv[1] if len(sys.argv) > 1 else "Rule-004-ENF-001"
    sys.exit(ScanMongoClientDirectAccessEnforcementWorker(enforcement_id).run())
