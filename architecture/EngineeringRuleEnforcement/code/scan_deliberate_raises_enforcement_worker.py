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
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

try:
    from .enforcement_worker import (
        EnforcementWorker, ViolationRecord, PROJECT_ROOT,
        EXIT_OK, EXIT_VIOLATIONS_FOUND, ChatHealthyException,
    )
except ImportError:
    from enforcement_worker import (  # noqa: E402
        EnforcementWorker, ViolationRecord, PROJECT_ROOT,
        EXIT_OK, EXIT_VIOLATIONS_FOUND, ChatHealthyException,
    )


# Rule-065 statement 2 requires one class. Anything else raised deliberately
# is a violation, so the check is an allowlist of one rather than a list of
# forbidden names - a blocklist cannot express "this class must be used", and
# the five-name version passed every custom exception ever written.
_REQUIRED: str = "ChatHealthyException"



def _raised_name(expr: ast.expr) -> str | None:
    """The name of the class being raised, however it is spelled."""
    if isinstance(expr, ast.Call):
        return _raised_name(expr.func)
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def _is_forbidden_raise(node: ast.Raise) -> str | None:
    """The class this raise uses when that class is not ChatHealthyException.

    A bare `raise` re-raises the exception being handled and is always
    allowed - Rule-065 statement 2 says so explicitly. `raise exc` where exc
    is a caught name is the same act written differently and is also allowed:
    it re-raises an object, it does not author a new one.
    """
    if node.exc is None:
        return None
    name = _raised_name(node.exc)
    if name is None:
        return None
    if name == _REQUIRED:
        return None
    # `raise exc` / `raise err` - re-raising a bound exception object, not
    # constructing one. Constructing is always a Call.
    if not isinstance(node.exc, ast.Call):
        return None
    return name



def _conversions_dropping_the_original(tree: ast.AST) -> list[tuple[int, str]]:
    """Conversions that discard the exception they were converting.

    The architecture is that an external exception keeps its native type
    until a boundary, where it becomes a ChatHealthyException. A conversion
    that does not carry the original into `exception=` throws the cause on
    the ground: the mode says what kind of failure it was, and nothing says
    what actually failed. The stack the operator needs is the one that no
    longer exists.

    A bare `raise` is untouched -- it re-raises the original itself.
    """
    hits: list[tuple[int, str]] = []
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name:
            continue
        for node in ast.walk(handler):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            if _raised_name(node.exc) != _REQUIRED:
                continue
            if any(kw.arg == "exception" for kw in node.exc.keywords):
                continue
            hits.append((node.lineno, handler.name))
    return hits



ENTRY_FUNCTION_MAX_LINES = 25


def _statement_count(fn: ast.AST) -> int:
    """How much the function does, counted in statements.

    Physical lines measure formatting, not work: one call wrapped across six
    lines for width is one thing done. So this counts statements -- calls,
    assignments, branches, loops, returns -- and ignores the docstring, which
    is not work.
    """
    total = 0
    for node in ast.walk(fn):
        if node is fn or not isinstance(node, ast.stmt):
            continue
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            continue
        total += 1
    return total


def _entry_point_exits(tree: ast.AST) -> tuple[set[int], list[tuple[int, str]]]:
    """Which exits are the entry-point convention, and which only look it.

    A process reports its outcome to whoever started it by exit code, and
    Python sets that status only through SystemExit. The manager reads its
    workers' codes, the hooks read the driver's, Azure reads a runbook's.
    So one shape is exempt, and it is a shape rather than a location:

      * the exit is the first statement under `if __name__ == "__main__":`
      * its argument calls a function defined in this module
      * that function is a driver -- no more than ENTRY_FUNCTION_MAX_LINES

    Everything else is an abort. An exit inside the entry function, an exit
    reached after other work has already run under the guard, or an entry
    function that is the program rather than a driver of it, all fail: the
    exemption exists so a process can report status, not so a body of work
    can sit where nothing will judge it.
    """
    lengths = {n.name: _statement_count(n) for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    # A module that never exits has no exit to police. A guard that simply
    # calls a function claims no exemption and is judged by nothing here.
    if not any(isinstance(n, ast.Call) and _is_exit_call(n.func)
               for n in ast.walk(tree)):
        return set(), []

    exempt: set[int] = set()
    faults: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and _is_main_guard(node.test)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        call = first.value if isinstance(first, ast.Expr) else None
        if isinstance(first, ast.Assign):
            call = first.value
        if not (isinstance(call, ast.Call) and _is_exit_call(call.func)):
            faults.append((first.lineno,
                           "@entry:the first statement under the main guard "
                           "does not exit"))
            continue
        exempt.add(call.lineno)
        inner = call.args[0] if call.args else None
        if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)):
            faults.append((call.lineno,
                           "@entry:the exit does not call an entry function"))
            continue
        name = inner.func.id
        exempt.add(inner.lineno)
        # The entry function is the one place an exit may be written. It is
        # what reports the process status, so it may choose the status.
        for fn in ast.walk(tree):
            if (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and fn.name == name):
                for inside in ast.walk(fn):
                    if isinstance(inside, ast.Call) and _is_exit_call(inside.func):
                        exempt.add(inside.lineno)
        length = lengths.get(name)
        if length is None:
            faults.append((call.lineno,
                           f"@entry:{name}() is not defined in this module"))
        elif length > ENTRY_FUNCTION_MAX_LINES:
            faults.append((call.lineno,
                           f"@entry:{name}() does {length} things; an entry "
                           f"function drives and does no more than "
                           f"{ENTRY_FUNCTION_MAX_LINES}"))
    return exempt, faults


def _is_exit_call(func: ast.expr) -> bool:
    if (isinstance(func, ast.Attribute) and func.attr == "exit"
            and isinstance(func.value, ast.Name) and func.value.id == "sys"):
        return True
    return isinstance(func, ast.Name) and func.id in ("exit", "quit")


def _is_main_guard(test: ast.expr) -> bool:
    """True for `__name__ == "__main__"`."""
    return (isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__")


def _exits_that_are_raises(tree: ast.AST) -> list[tuple[int, str]]:
    """Calls that raise SystemExit without writing the word raise.

    sys.exit(n) constructs SystemExit(n) and raises it: the stack unwinds,
    finally blocks run, context managers exit, and a bare except catches it.
    It is the same act as `raise SystemExit(n)` in a spelling that produces
    no ast.Raise node, so a check that walks raises alone cannot see it --
    and eleven sites were converted from one spelling to the other and
    reported as compliant.

    exit() and quit() are the interactive builtins doing the same thing.
    """
    exempt, faults = _entry_point_exits(tree)

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if node.lineno in exempt:
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "exit"
                and isinstance(func.value, ast.Name) and func.value.id == "sys"):
            hits.append((node.lineno, "sys.exit()"))
        elif isinstance(func, ast.Name) and func.id in ("exit", "quit"):
            hits.append((node.lineno, f"{func.id}()"))
    return hits, faults


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
    for lineno, caught in _conversions_dropping_the_original(tree):
        hits.append((lineno, f"@drop:{caught}"))
    exits, entry_faults = _exits_that_are_raises(tree)
    for lineno, how in exits:
        hits.append((lineno, f"@exit:{how}"))
    hits.extend(entry_faults)
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
            if name.startswith("@entry:"):
                message = name.split(":", 1)[1]
            elif name.startswith("@exit:"):
                how = name.split(":", 1)[1]
                message = (
                    f"{how} raises SystemExit without writing the word raise: "
                    f"it constructs the exception, unwinds the stack, runs "
                    f"finally blocks and is catchable. It is a deliberate "
                    f"raise of a built-in in another spelling. Raise "
                    f"ChatHealthyException(mode=..., message=...) instead."
                )
            elif name.startswith("@drop:"):
                caught = name.split(":", 1)[1]
                message = (
                    f"this catch converts {caught!r} to ChatHealthyException "
                    f"and does not carry it: pass exception={caught}. An "
                    f"external exception keeps its native type until a "
                    f"boundary converts it, and a conversion that drops the "
                    f"original leaves the mode saying what kind of failure "
                    f"it was and nothing saying what failed."
                )
            else:
                message = (
                    f"deliberate raise of built-in {name!r}; "
                    f"EPIC-008-F-002-S-009-REQ-B-002 forbids this — use "
                    f"ChatHealthyException(mode=..., message=...) instead."
                )
            violations.append(ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id="Rule-003",
                resource=f"{file_path}:{lineno}",
                message=message,
            ))
        return violations


def main() -> int:
    """Drive the program and report its status.

    The exit lives here because this is the function the guard
    calls, and a process reports its outcome by exit code.
    """
    enforcement_id = sys.argv[1] if len(sys.argv) > 1 else "Rule-065-ENF-002"
    return ScanDeliberateRaisesEnforcementWorker(enforcement_id).run()


if __name__ == "__main__":
    sys.exit(main())
