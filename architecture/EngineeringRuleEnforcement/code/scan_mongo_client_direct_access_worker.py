"""scan_mongo_client_direct_access_worker.py — Rule-004 worker.

Enforces EPIC-008-F-002-S-010-REQ-B-007 (direct MongoClient forbidden) and
REQ-B-012 (direct MONGO_* env var reads forbidden). All MongoDB access MUST
route through ChatHealthyMongoUtilities().getConnection(cert_name).

Mechanism: AST-based scan (no regex per Rule-008 statement 4). For every
in-scope staged .py file, parse and walk all Call/Subscript nodes; reject when:
  1. Call target is MongoClient (bare or qualified)
  2. os.environ accesses MONGO_* variables (get, pop, setdefault, subscript)
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


def _is_mongo_env_var(name: str) -> bool:
    """Check if name matches MONGO_* connection string pattern."""
    if name == "MONGO_connectionString" or name == "MONGO_FRONTEND_connectionString":
        return True
    if name.startswith("MONGO_CLUSTER_") and name.endswith("_connectionString"):
        return True
    return False


def _is_mongo_env_access(node: ast.expr) -> str | None:
    """If node accesses MONGO_* env var, return the var name; else None.

    Patterns matched:
      - os.environ.get("MONGO_*")
      - os.environ.pop("MONGO_*")
      - os.environ.setdefault("MONGO_*")
      - os.environ["MONGO_*"]
    """
    # os.environ.get() / .pop() / .setdefault() in Call nodes
    if isinstance(node, ast.Call):
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "os"
            and func.value.attr == "environ"
            and func.attr in ("get", "pop", "setdefault")
        ):
            if node.args and isinstance(node.args[0], ast.Constant):
                key = node.args[0].value
                if isinstance(key, str) and _is_mongo_env_var(key):
                    return key
    return None


def _find_violations(source: str) -> list[tuple[int, str]]:
    """Return list of (lineno, violation_type) for direct MongoClient and MONGO_* reads.
    violation_type is 'MongoClient' or 'MONGO_env_var' or 'MONGO_env_subscript'.
    Unparseable source returns []."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    hits: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        # Direct MongoClient() call
        if isinstance(node, ast.Call) and _is_mongo_client_call(node):
            hits.append((node.lineno, "MongoClient"))

        # os.environ.get/pop/setdefault("MONGO_*")
        if isinstance(node, ast.Call):
            env_var = _is_mongo_env_access(node)
            if env_var:
                hits.append((node.lineno, "MONGO_env_var"))

        # os.environ["MONGO_*"]
        if isinstance(node, ast.Subscript):
            if (
                isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "os"
                and node.value.attr == "environ"
                and isinstance(node.slice, ast.Constant)
            ):
                key = node.slice.value
                if isinstance(key, str) and _is_mongo_env_var(key):
                    hits.append((node.lineno, "MONGO_env_subscript"))

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
        for lineno, violation_type in _find_violations(source):
            if violation_type == "MongoClient":
                message = (
                    "Direct MongoClient(...) instantiation is forbidden "
                    "outside FrontEndApplicationLib/src/chathealthy_frontend_lib/"
                    "mongo_utilities.py per EPIC-008-F-002-S-010-REQ-B-007. "
                    "Use ChatHealthyMongoUtilities().getConnection(cert_name) instead."
                )
            else:  # MONGO_env_var or MONGO_env_subscript
                message = (
                    "Direct read of MongoDB environment variable is forbidden "
                    "per EPIC-008-F-002-S-010-REQ-B-012. "
                    "Use ChatHealthyMongoUtilities().getConnection(cert_name) instead."
                )

            violations.append(ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id="Rule-004",
                resource=f"{file_path}:{lineno}",
                message=message,
            ))
        return violations


if __name__ == "__main__":
    enforcement_id = sys.argv[1] if len(sys.argv) > 1 else "Rule-004-ENF-001"
    sys.exit(ScanMongoClientDirectAccessEnforcementWorker(enforcement_id).run())
