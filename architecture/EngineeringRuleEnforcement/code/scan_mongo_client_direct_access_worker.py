"""scan_mongo_client_direct_access_worker.py — Rule-004 worker.

Enforces EPIC-008-F-002-S-010-REQ-B-007: direct MongoClient(...) instantiation
is forbidden in any ChatHealthy.ai-authored Python file except within
FrontEndApplicationLib/src/chathealthy_frontend_lib/mongo_utilities.py.

All MongoDB connections MUST use ChatHealthyMongoUtilities().getConnection(cert_name).

Mechanism: AST-based scan (no regex per Rule-008 statement 4). For every
in-scope staged .py file, parse and walk every Call node; reject when the
call target is MongoClient (either bare `MongoClient(...)` or qualified like
`pymongo.MongoClient(...)`).
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

# Packages that stand in for a real MongoDB client. A double presents no
# certificate, so it matches no $external user and carries no role: code
# exercised against one has never had its authorization tested. Detecting
# only the name `MongoClient` let these through, because mongomock's client
# is a different symbol from a different package and the letter of the check
# did not reach it.
_FAKE_MODULES = ("mongomock", "mongomock_motor", "pytest_mongodb", "mongobox")


def _is_mongo_client_call(node: ast.Call) -> bool:
    """True when the Call target is the MongoClient name (bare or
    qualified). Bare: `MongoClient(...)`. Qualified: `pymongo.MongoClient(...)`."""
    func = node.func
    if isinstance(func, ast.Name) and func.id == _FORBIDDEN_NAME:
        return True
    if isinstance(func, ast.Attribute) and func.attr == _FORBIDDEN_NAME:
        return True
    return False


def _fake_root(node: ast.AST) -> str | None:
    """The faking package a dotted expression is rooted at, if any."""
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Name) and node.id in _FAKE_MODULES:
        return node.id
    return None


def _find_mongo_client_calls(source: str) -> list[tuple[int, str]]:
    """Every place this file acquires a Mongo client other than via the
    utility, as (line, what). Raises SyntaxError on unparseable source; the
    caller reports it."""
    # Deliberately does not swallow SyntaxError. A file that cannot be parsed
    # cannot be certified compliant, and returning [] here meant unparseable
    # Python passed every rule silently -- an exception granted by code rather
    # than by the exclusion list. The caller reports it as a violation.
    tree = ast.parse(source)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _is_mongo_client_call(node):
                hits.append((node.lineno, "direct MongoClient(...) construction"))
                continue
            root = _fake_root(node.func)
            if root:
                hits.append((node.lineno, f"call into {root}, a MongoDB fake"))
                continue
        # Importing a fake at all: the import is the intent, and a file that
        # imports one has somewhere decided a certificate is optional.
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _FAKE_MODULES:
                    hits.append((node.lineno,
                                 f"import of {alias.name}, a MongoDB fake"))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in _FAKE_MODULES:
                hits.append((node.lineno,
                             f"import from {node.module}, a MongoDB fake"))
        # pytest.importorskip("mongomock") -- the same acquisition wearing a
        # skip, which additionally makes the test disappear rather than fail.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "importorskip":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and arg.value.split(".")[0] in _FAKE_MODULES:
                    hits.append((node.lineno,
                                 f"importorskip({arg.value!r}), a MongoDB fake"))
    return sorted(set(hits))


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
        """The file array the Rule-065 driver handed down.

        This worker owns no git knowledge. What a commit answers for is one
        decision, and the driver makes it once for every subordinate.
        """
        return self.files

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
        violations: list[ViolationRecord] = []
        try:
            source = absolute_path.read_text(encoding="utf-8")
            call_lines = _find_mongo_client_calls(source)
        except (UnicodeDecodeError, SyntaxError) as exc:
            # Unreadable or unparseable is not compliant, it is unknown. Both
            # used to return [] and read as a clean pass.
            return [ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id="Rule-004",
                resource=file_path,
                message=(
                    f"file could not be scanned ({type(exc).__name__}: "
                    f"{str(exc)[:120]}). A file that cannot be read cannot be "
                    f"certified compliant; fix the file or put it on this "
                    f"rule's exclusion list."
                ),
                severity="error",
            )]
        for lineno, what in call_lines:
            violations.append(ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id="Rule-004",
                resource=f"{file_path}:{lineno}",
                message=(
                    f"{what} — forbidden outside FrontEndApplicationLib/src/"
                    f"chathealthy_frontend_lib/mongo_utilities.py per "
                    f"EPIC-008-F-002-S-010-REQ-B-007. A client obtained any "
                    f"other way presents no certificate, so it matches no "
                    f"$external user and carries no role: the code's "
                    f"authorization is never tested. Use "
                    f"ChatHealthyMongoUtilities().getConnection(identity, "
                    f"cluster) — tests connect as DevOpsUser against a test "
                    f"database they create and drop. There is no test-file "
                    f"exemption."
                ),
            ))
        return violations


def main(argv: list[str] | None = None) -> int:
    return ScanMongoClientDirectAccessEnforcementWorker.main(argv)


if __name__ == "__main__":
    sys.exit(main())
