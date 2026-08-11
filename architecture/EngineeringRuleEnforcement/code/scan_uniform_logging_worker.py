"""scan_uniform_logging_worker.py — Rule-005 worker.

Enforces four independent checks from EPIC-003-F-005:
  (a) REQ-B-002 — outside the canonical surface (logging_service.py), no
      ChatHealthy.ai-authored Python file may call logging.basicConfig(),
      logging.getLogger(), or print().
  (b) REQ-B-007 — the `exc=` argument passed to any ChatHealthyLoggingService
      log method MUST be a ChatHealthyException (or a name bound by an
      `except ChatHealthyException as <name>:` clause).
  (c) REQ-B-010 — any function/method whose body contains a
      `raise _ch_exception()(...)` may not call any
      ChatHealthyLoggingService log method in the same function body.
  (d) REQ-B-002 (import-form) — outside the canonical surface, no file may
      `import logging`, `import logging as <alias>`, or `from logging import …`.
      The stdlib logging module reaches the application only through
      logging_service.py; every other file uses ChatHealthyLoggingService.

Mechanism: AST-based scan (no regex per Rule-008 statement 4). Reject any
violations found in staged files.
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


FORBIDDEN_STDLIB_CALLS = frozenset({"basicConfig", "getLogger"})
FORBIDDEN_BARE_NAMES = frozenset({"print"})
LOG_METHODS = frozenset({
    "debug", "info", "warning", "error", "critical", "exception",
})



def _ch_exception():
    """ChatHealthyException, resolved without assuming the library is on the
    path. Enforcement workers are spawned as bare scripts by the manager."""
    import sys as _sys, pathlib as _pl
    for _p in _pl.Path(__file__).resolve().parents:
        if (_p / ".git").exists():
            _lib = _p / "ChatHealthyLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            break
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException

def is_stdlib_logging_call(node: ast.Call) -> str | None:
    """Return the forbidden name when node is logging.basicConfig() /
    logging.getLogger() / print(), else None. Both qualified
    (logging.basicConfig) and bare (basicConfig after `from logging import …`)
    are caught only for the qualified form here; bare-name detection is
    done by symbol-table walk in _bare_forbidden_calls."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if (
            isinstance(func.value, ast.Name)
            and func.value.id == "logging"
            and func.attr in FORBIDDEN_STDLIB_CALLS
        ):
            return f"logging.{func.attr}"
    if isinstance(func, ast.Name) and func.id in FORBIDDEN_BARE_NAMES:
        return func.id
    return None


def bare_imports_from_logging(tree: ast.Module) -> set[str]:
    """Return names imported via `from logging import …` that fall under
    _FORBIDDEN_STDLIB_CALLS. We use these to flag bare calls like
    basicConfig(...) when basicConfig was imported from logging."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "logging":
            for alias in node.names:
                name = alias.asname or alias.name
                if alias.name in FORBIDDEN_STDLIB_CALLS:
                    bound.add(name)
    return bound


def is_log_method_call(node: ast.Call) -> bool:
    """True when node is `<something>.<one of LOG_METHODS>(...)`. The
    <something> is taken to refer to a ChatHealthyLoggingService instance
    by convention (`log = ChatHealthyLoggingService()` is the standard
    pattern in our codebase). False-positives on other objects with
    `.info`/etc. methods are accepted in v1 as harmless — they don't get
    a ChatHealthyException-typed exc= argument anyway."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in LOG_METHODS
        and isinstance(func.value, ast.Name)
    )


def raises_chathealthy_exception(node: ast.Raise) -> bool:
    """True when node is `raise _ch_exception()(...)` (with or without
    a bound exception name)."""
    exc = node.exc
    if exc is None:
        return False
    # `raise _ch_exception()(...)`
    if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name)\
            and exc.func.id == "ChatHealthyException":
        return True
    # `raise ChatHealthyException`
    if isinstance(exc, ast.Name) and exc.id == "ChatHealthyException":
        return True
    return False


def bound_chathealthy_exception_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Return the set of names bound by `except ChatHealthyException as <n>:`
    clauses inside the function body."""
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.ExceptHandler) and n.name:
            t = n.type
            if isinstance(t, ast.Name) and t.id == "ChatHealthyException":
                names.add(n.name)
    return names


def exc_arg_resolves_to_chathealthy(
    call: ast.Call,
    bound_ch_names: set[str],
) -> bool:
    """True when the `exc=` keyword argument on a log method call resolves
    to a ChatHealthyException. Three accepted forms:
      1. exc=ChatHealthyException(...)
      2. exc=<name> where <name> was bound by `except ChatHealthyException as <name>:`
      3. exc=None  (the kwarg defaults to None per the design; explicit None is fine)
    """
    for kw in call.keywords:
        if kw.arg != "exc":
            continue
        v = kw.value
        if isinstance(v, ast.Constant) and v.value is None:
            return True
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name)\
                and v.func.id == "ChatHealthyException":
            return True
        if isinstance(v, ast.Name) and v.id in bound_ch_names:
            return True
        return False
    # No exc= keyword: not a violation of this check.
    return True


def check_a_violations(source: str, file_path: str) -> list[tuple[int, str]]:
    """Check (a): logging.basicConfig / logging.getLogger / print calls.
    Returns list of (lineno, label)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    bare = bare_imports_from_logging(tree)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        label = is_stdlib_logging_call(node)
        if label is not None:
            hits.append((node.lineno, label))
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in bare:
            hits.append((node.lineno, f"{func.id}() (bare-imported from logging)"))
    return hits


def check_b_violations(source: str) -> list[tuple[int, str]]:
    """Check (b): exc= arg on log methods must be ChatHealthyException."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: list[tuple[int, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bound = bound_chathealthy_exception_names(fn)
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            if not is_log_method_call(n):
                continue
            if not exc_arg_resolves_to_chathealthy(n, bound):
                hits.append((
                    n.lineno,
                    "log method exc= argument is not a ChatHealthyException",
                ))
    return hits


def check_c_violations(source: str) -> list[tuple[int, str]]:
    """Check (c): throw-doesn't-log. In any function body that raises a
    ChatHealthyException, no log method may be called."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: list[tuple[int, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        raises_ch = any(
            isinstance(n, ast.Raise) and raises_chathealthy_exception(n)
            for n in ast.walk(fn)
        )
        if not raises_ch:
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and is_log_method_call(n):
                hits.append((
                    n.lineno,
                    f"log call inside function {fn.name!r} that raises "
                    f"ChatHealthyException — the catcher logs, not the thrower",
                ))
    return hits


def check_d_violations(source: str) -> list[tuple[int, str]]:
    """Check (d): no stdlib `logging` import outside the canonical surface.

    Forbidden import forms:
      * `import logging`
      * `import logging as <alias>`
      * `from logging import <name>` / `from logging import *`
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "logging" or alias.name.startswith("logging."):
                    asname = alias.asname
                    label = (
                        f"import logging as {asname}" if asname
                        else f"import {alias.name}"
                    )
                    hits.append((node.lineno, label))
        elif isinstance(node, ast.ImportFrom):
            if node.module == "logging":
                hits.append((
                    node.lineno,
                    f"from logging import {', '.join(a.name for a in node.names)}",
                ))
    return hits


def all_violations(source: str, file_path: str) -> list[tuple[int, str]]:
    return (
        check_a_violations(source, file_path)
        + check_b_violations(source)
        + check_c_violations(source)
        + check_d_violations(source)
    )


class ScanUniformLoggingEnforcementWorker(EnforcementWorker):
    """Rule-005: uniform logging discipline."""

    SCOPE_DEFAULTS: dict[str, bool] = {
        "_scan_uniform_logging": False,
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
            if not self.is_in_scope(file_path, "_scan_uniform_logging"):
                continue
            for v in self._scan_uniform_logging(file_path):
                self._emit_violation(v)
                self.violation_count += 1
                any_violations = True
        return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK

    def _scan_uniform_logging(self, file_path: str) -> list[ViolationRecord]:
        absolute_path = (PROJECT_ROOT / file_path).resolve()
        if not absolute_path.is_file():
            return []
        try:
            staged_text = absolute_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return []
        staged_hits = all_violations(staged_text, file_path)
        if not staged_hits:
            return []
        violations: list[ViolationRecord] = []
        for lineno, label in staged_hits:
            violations.append(ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id="Rule-005",
                resource=f"{file_path}:{lineno}",
                message=(
                    f"{label}. Use ChatHealthyLoggingService instead."
                ),
            ))
        return violations


if __name__ == "__main__":
    enforcement_id = sys.argv[1] if len(sys.argv) > 1 else "Rule-065-ENF-004"
    sys.exit(ScanUniformLoggingEnforcementWorker(enforcement_id).run())
