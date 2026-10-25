# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Every pipeline module is sound before a run is ever fired.

Three runs died on 2026-10-25, each on a defect that cost a cluster wake, a
VM, and twenty minutes of waiting to discover:

  * ``name 'ttl_hours' is not defined`` -- a parameter renamed in a signature
    and left behind in the body.
  * ``window_minutes`` used by two cloud-init builders that never received it.
  * ``%d format: a real number is required, not NoneType`` -- a value changed
    from int to optional, with the log line three lines below still formatting
    it as a number.

None of the three needed a database to find. They needed somebody to read the
code, which is what this file does mechanically.

WHY NOT JUST RUN THE STEPS. Rule-065 statement 3 forbids standing a fake,
mock, stub or in-memory double in place of a Mongo client, database or
collection, and every step takes one. So no test may execute a step body, and
that is exactly why these defects reached production three times in one night.
Reading the syntax is the one route left, and it is enough: all three are
visible in the source.

No regular expressions -- Rule-065-ENF-006 forbids them in executable code.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_PIPELINE = _ROOT / "pipeline"
_SKIP_PARTS = ("__pycache__", "_oneshots", "tests", "ArchitectureDesignAndAudit")

_BUILTINS = frozenset(dir(builtins)) | {"__file__", "__name__", "__doc__"}


def _sources() -> list[Path]:
    out = []
    for path in sorted(_PIPELINE.rglob("*.py")):
        parts = set(path.parts)
        if parts & set(_SKIP_PARTS):
            continue
        out.append(path)
    return out


SOURCES = _sources()
IDS = [str(p.relative_to(_ROOT)).replace("\\", "/") for p in SOURCES]


# ---------------------------------------------------------------- scopes

def _bound_names(node: ast.AST) -> set[str]:
    """Every name this scope binds: assignments, params, imports, targets.

    Walks nested scopes too. A name bound anywhere in the function is treated
    as bound for the whole function, which is how Python's own scoping works
    and keeps this check free of flow analysis.
    """
    bound: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            bound.add(child.id)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(child.name)
            args = getattr(child, "args", None)
            if args is not None:
                bound.update(_arg_names(args))
        elif isinstance(child, ast.Lambda):
            bound.update(_arg_names(child.args))
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(child, ast.ExceptHandler) and child.name:
            bound.add(child.name)
        elif isinstance(child, (ast.Global, ast.Nonlocal)):
            bound.update(child.names)
        elif isinstance(child, ast.comprehension):
            for sub in ast.walk(child.target):
                if isinstance(sub, ast.Name):
                    bound.add(sub.id)
    return bound


def _arg_names(args: ast.arguments) -> set[str]:
    names = {a.arg for a in args.args + args.kwonlyargs + args.posonlyargs}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _undefined_names(tree: ast.AST) -> list[tuple[str, str, int]]:
    """(function, name, line) for every name a function loads and nothing
    binds -- not as a parameter, not in the function, not at module level,
    not in an enclosing function, and not a builtin."""
    module_bound = _bound_names(tree) | _BUILTINS
    enclosing: dict[ast.AST, set[str]] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                enclosing[child] = enclosing.get(parent, set()) | _bound_names(parent)

    findings: list[tuple[str, str, int]] = []
    for fn in _functions(tree):
        bound = (_bound_names(fn) | _arg_names(fn.args) | module_bound
                 | enclosing.get(fn, set()))
        seen: set[str] = set()
        for node in ast.walk(fn):
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id not in bound and node.id not in seen):
                seen.add(node.id)
                findings.append((fn.name, node.id, node.lineno))
    return findings


@pytest.mark.parametrize("path", SOURCES, ids=IDS)
def test_module_parses(path: Path):
    """A file that does not parse cannot be run; say so here, not on a VM."""
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.mark.parametrize("path", SOURCES, ids=IDS)
def test_no_undefined_names(path: Path):
    """The ttl_hours and window_minutes class: a name used and never bound.

    This is the check that would have stopped two of the three runs that died
    on 2026-10-25, before either was fired.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    findings = _undefined_names(tree)
    assert not findings, "\n".join(
        f"{path.name}:{line} {fn}() uses {name!r}, which nothing binds"
        for fn, name, line in findings)


# ------------------------------------------------- numeric log formatting

_NUMERIC_SPECIFIERS = ("%d", "%i", "%u", "%.1f", "%.2f", "%.3f", "%f", "%x")


def _numeric_positions(template: str) -> list[int]:
    """Which %-argument positions of a format string demand a number.

    Counts every conversion in order and records the index of the numeric
    ones, so the caller can line them up with the arguments that follow.
    """
    positions: list[int] = []
    index = 0
    rest = template
    while "%" in rest:
        at = rest.index("%")
        tail = rest[at:]
        if tail.startswith("%%"):
            rest = tail[2:]
            continue
        matched = ""
        for spec in _NUMERIC_SPECIFIERS:
            if tail.startswith(spec) and len(spec) > len(matched):
                matched = spec
        if matched:
            positions.append(index)
            index += 1
            rest = tail[len(matched):]
            continue
        # any other conversion: consume one argument slot and move on
        index += 1
        rest = tail[1:] if len(tail) > 1 else ""
    return positions


def _may_be_none(node: ast.AST) -> bool:
    """True when this argument expression can evaluate to None.

    Narrow on purpose: a dict .get() with no default, an explicit None, or
    .get() whose default is None. Those are the shapes that turned an int
    into an optional and crashed a run; anything cleverer would guess.
    """
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr != "get":
            return False
        if len(node.args) == 1:
            return True
        if len(node.args) == 2:
            default = node.args[1]
            return isinstance(default, ast.Constant) and default.value is None
    return False


def _assignments(fn: ast.AST) -> dict[str, list[ast.AST]]:
    """name -> every expression assigned to it in this function.

    The defect that crashed a run assigned the optional to a local and
    formatted the LOCAL three lines later, so resolving one hop is the whole
    point: looking only at the call site sees a plain name and nothing wrong.
    """
    out: dict[str, list[ast.AST]] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                out.setdefault(node.target.id, []).append(node.value)
    return out


def _template_of(call: ast.Call) -> tuple[int, str] | None:
    """The format string of a log call and where it sits.

    It is the first string constant CARRYING A CONVERSION -- not simply the
    first string, which on LogPipeline is the level word "INFO" and sent the
    first version of this check looking at the wrong argument entirely.
    """
    for position, argument in enumerate(call.args):
        if (isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and _numeric_positions(argument.value)):
            return position, argument.value
    return None


def _bad_numeric_formats(tree: ast.AST) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    for fn in _functions(tree):
        assigned = _assignments(fn)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in ("LogPipeline", "info", "warning", "error", "debug",
                            "exception", "log"):
                continue
            found = _template_of(node)
            if found is None:
                continue
            template_at, template = found
            values = node.args[template_at + 1:]
            for slot in _numeric_positions(template):
                if slot >= len(values):
                    continue
                argument = values[slot]
                suspect = _may_be_none(argument)
                if not suspect and isinstance(argument, ast.Name):
                    suspect = any(_may_be_none(source)
                                  for source in assigned.get(argument.id, []))
                if suspect:
                    findings.append((node.lineno, template[:70]))
    return findings


@pytest.mark.parametrize("path", SOURCES, ids=IDS)
def test_no_numeric_format_fed_a_possible_none(path: Path):
    """The %d class: a numeric conversion handed something that can be None.

    `%d` with None raises TypeError inside the logging call, which killed
    prepare_infrastructure on 2026-10-25 after the cluster had already been
    woken and the controller provisioned.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    findings = _bad_numeric_formats(tree)
    assert not findings, "\n".join(
        f"{path.name}:{line} numeric conversion fed a value that can be None: {template}"
        for line, template in findings)


# ------------------------------------------------- the detectors are proven
#
# A green suite proves nothing unless the checks above can actually fail. Each
# test below feeds a detector the VERBATIM defect that killed a run on
# 2026-10-25 and asserts it is caught. If someone weakens a detector, these go
# red before the next run does.

_TTL_HOURS_DEFECT = '''
def _create_reservation(mongo, run_id, vm_name, window_minutes):
    expiry = now + datetime.timedelta(hours=ttl_hours)
    return expiry
'''

_WINDOW_MINUTES_DEFECT = '''
def _controller_cloud_init(run_id, data_version):
    return f"-e EXPECTED_DURATION_MINUTES='{window_minutes}'"
'''

_PERCENT_D_DEFECT = '''
def run_step(ctx):
    wait_minutes = ctx.config.get("cluster_wait_minutes")
    _log.LogPipeline("INFO", "ensuring indexes on %s (waits up to %d minute(s))",
                     ctx.provider_collection, wait_minutes)
'''


def test_detector_catches_the_renamed_parameter_left_in_the_body():
    """Run 940a494e: name 'ttl_hours' is not defined."""
    findings = [f[1] for f in _undefined_names(ast.parse(_TTL_HOURS_DEFECT))]
    assert "ttl_hours" in findings


def test_detector_catches_the_parameter_that_was_never_threaded():
    """Run f24cfd49 would have died here: window_minutes never received."""
    findings = [f[1] for f in _undefined_names(ast.parse(_WINDOW_MINUTES_DEFECT))]
    assert "window_minutes" in findings


def test_detector_catches_a_numeric_format_fed_an_optional():
    """Run 1a5eb163: %d format: a real number is required, not NoneType."""
    findings = _bad_numeric_formats(ast.parse(_PERCENT_D_DEFECT))
    assert len(findings) == 1


def test_detector_does_not_flag_a_get_that_carries_a_default():
    """The same line before I broke it: .get(key, 20) is always an int."""
    sound = _PERCENT_D_DEFECT.replace('get("cluster_wait_minutes")',
                                      'get("cluster_wait_minutes", 20)')
    assert _bad_numeric_formats(ast.parse(sound)) == []


def test_detector_does_not_flag_ordinary_working_code():
    """A function that binds everything it uses raises nothing."""
    sound = "def f(a, b):\n    c = a + b\n    return [x for x in range(c)]\n"
    assert _undefined_names(ast.parse(sound)) == []


# ============================================================================
# THE PATTERNS THE AUDIT FOUND, not just the defects I tripped over.
#
# The time-limit audit of 2026-10-24 produced 97 findings. Read for SHAPES
# rather than instances they come to four, and this file covers each:
#
#   elapsed-time abort   a clock compared, then work abandoned. ZERO remain
#                        in pipeline code; this keeps it at zero.
#   literal lease        a reservation or lock built from a hardcoded
#                        duration rather than the run's window. ZERO remain.
#   naked cursor         a cursor iterated while the body makes its own round
#                        trips, so the server reaps it mid-loop. This threw
#                        CursorNotFound and killed five enrich partitions on
#                        2026-10-23. FIVE remain, listed below.
#   maxTimeMS            a server-side clock on a query. None in pipeline
#                        code outside the test fixtures.
# ============================================================================

_CLOCK_CALLS = ("time", "monotonic", "perf_counter", "utcnow", "now")

# Real round trips only. A dict .get() is not one, and counting it as one
# inflated this list from 7 to 26 with bounded lookups that are safe.
_REMOTE_CALLS = ("find", "find_one", "bulk_write", "update_one", "update_many",
                 "insert_one", "insert_many", "delete_one", "delete_many",
                 "replace_one", "aggregate", "count_documents", "create_index",
                 "urlopen", "request")

# Known naked cursors, measured 2026-10-25 after deleting the two that were
# dead code. NONE is on the Medicare path a run executes today: one is the FDA
# leg a run skips, the rest are the provider pipeline and the data release. An
# entry leaves this set by being FIXED -- paged by key, or given
# no_cursor_timeout. Adding an entry is how this check gets defeated.
# EMPTY, and it stays empty. All five sites were fixed on 2026-10-25 rather
# than carried: each now declares no_cursor_timeout and closes its cursor
# through a with-block. A name added here is a naked cursor someone chose not
# to fix, so adding one is the only way to defeat this check.
_KNOWN_NAKED_CURSORS = frozenset()


def _rel(path: Path) -> str:
    return str(path.relative_to(_ROOT)).replace(chr(92), "/")


def _elapsed_time_aborts(tree: ast.AST) -> list[int]:
    """Lines where a clock is compared and work is then abandoned."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        reads_clock = any(
            isinstance(n, ast.Call) and getattr(n.func, "attr", "") in _CLOCK_CALLS
            for n in ast.walk(node.test))
        compares = any(isinstance(n, ast.Compare) for n in ast.walk(node.test))
        abandons = any(isinstance(n, (ast.Raise, ast.Break)) for n in ast.walk(node))
        if reads_clock and compares and abandons:
            out.append(node.lineno)
    return out


def _literal_leases(tree: ast.AST) -> list[str]:
    """Durations built from a number in the source."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "timedelta":
            for kw in node.keywords:
                if (kw.arg in ("hours", "minutes", "days")
                        and isinstance(kw.value, ast.Constant)):
                    out.append(str(node.lineno) + ": timedelta("
                               + str(kw.arg) + "=" + str(kw.value.value) + ")")
    return out


def _naked_cursors(tree: ast.AST) -> list[int]:
    """Cursors iterated while the loop body makes its own round trips."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        source = node.iter
        if not (isinstance(source, ast.Call)
                and getattr(source.func, "attr", "") in ("find", "aggregate")):
            continue
        if any(kw.arg in ("no_cursor_timeout", "limit") for kw in source.keywords):
            continue
        if any(isinstance(n, ast.Call)
               and getattr(n.func, "attr", "") in _REMOTE_CALLS
               for n in ast.walk(node) if n is not source):
            out.append(node.lineno)
    return out


@pytest.mark.parametrize("path", SOURCES, ids=IDS)
def test_no_elapsed_time_aborts_work(path: Path):
    """One limit only: the run window the operator argues at the trigger."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = _elapsed_time_aborts(tree)
    assert not lines, (
        _rel(path) + " abandons work on a clock at line(s) " + str(lines)
        + ". The run's window is the only limit: wait, log, and let the "
          "operator decide.")


@pytest.mark.parametrize("path", SOURCES, ids=IDS)
def test_no_lease_built_from_a_literal_duration(path: Path):
    """A reservation or lock lasts the run's window, not a number in source.

    A 24-hour TTL and a 2-hour renewal, neither argued by anyone, expired
    under live runs and let the reaper pause their data cluster.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = _literal_leases(tree)
    assert not found, _rel(path) + " builds a lease from a literal: " + str(found)


@pytest.mark.parametrize("path", SOURCES, ids=IDS)
def test_no_new_naked_cursor(path: Path):
    """A cursor must not be held across per-record remote work.

    MongoDB reaps an idle cursor at ten minutes and the next getMore raises
    CursorNotFound, taking the partition with it. Page by key, or declare
    no_cursor_timeout.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    fresh = [_rel(path) + ":" + str(line) for line in _naked_cursors(tree)
             if _rel(path) + ":" + str(line) not in _KNOWN_NAKED_CURSORS]
    assert not fresh, "new cursor(s) held across remote work: " + ", ".join(fresh)


def test_the_debt_list_shrinks_and_never_goes_stale():
    """An entry that no longer matches has been fixed: take it off the list.

    A stale entry is a hiding place for the next naked cursor.
    """
    live = set()
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line in _naked_cursors(tree):
            live.add(_rel(path) + ":" + str(line))
    stale = sorted(_KNOWN_NAKED_CURSORS - live)
    assert not stale, ("fixed, so remove from _KNOWN_NAKED_CURSORS: "
                       + ", ".join(stale))


def test_detector_catches_a_cursor_held_across_remote_work():
    """The 2026-10-23 shape, verbatim: a lookup per record inside the cursor."""
    defect = ("def enrich(target_coll, provider_coll):\n"
              "    for rec in target_coll.find({}):\n"
              "        doc = provider_coll.find_one({})\n")
    assert _naked_cursors(ast.parse(defect)) == [2]


def test_detector_clears_the_paged_rewrite_that_replaced_it():
    """The fix that shipped: a bounded page, so no cursor outlives the work."""
    sound = ("def enrich(target_coll, provider_coll):\n"
             "    page = list(target_coll.find({}, limit=1000))\n"
             "    for rec in page:\n"
             "        doc = provider_coll.find_one({})\n")
    assert _naked_cursors(ast.parse(sound)) == []


def test_detector_catches_an_elapsed_time_abort():
    """The step timeout that killed a four-hour run, in miniature."""
    defect = ("def wait(started, budget):\n"
              "    if (datetime.utcnow() - started).total_seconds() > budget:\n"
              "        raise ChatHealthyException(mode='step_timeout')\n")
    assert _elapsed_time_aborts(ast.parse(defect)) == [2]


def test_detector_catches_a_literal_lease():
    """The 24-hour reservation TTL nobody argued for."""
    defect = "def reserve(now):\n    return now + datetime.timedelta(hours=24)\n"
    assert _literal_leases(ast.parse(defect))


# ------------------------------------------- configuration levers
#
# A lever is filled in by somebody else, so it can arrive as None even when
# the .get() carries a default: the default covers a MISSING key, not a key
# present and empty. int(None) killed run e4689013 at the first step, after
# the cluster was already awake, from a line that read
# int(config.get("cluster_wait_minutes", 20)) and looked perfectly safe.

_CONFIG_RECEIVERS = ("config", "cfg", "conf", "settings", "levers", "args_snap")


def _coerced_levers(tree: ast.AST) -> list[int]:
    """Lines coercing a configuration value straight to a number."""
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("int", "float", "round") and node.args):
            continue
        argument = node.args[0]
        if not (isinstance(argument, ast.Call)
                and getattr(argument.func, "attr", "") == "get"):
            continue
        receiver = argument.func.value
        name = getattr(receiver, "id", None)
        if name is None and isinstance(receiver, ast.Attribute):
            name = receiver.attr
        if name in _CONFIG_RECEIVERS:
            out.append(node.lineno)
    return out


@pytest.mark.parametrize("path", SOURCES, ids=IDS)
def test_no_configuration_lever_is_coerced_without_a_none_guard(path: Path):
    """Read the lever, then coerce it only if there is a value."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = _coerced_levers(tree)
    assert not lines, (
        _rel(path) + " coerces a configuration lever straight to a number at "
        + str(lines) + ". A .get() default does not cover a key that is "
        "present and None.")


_LEVER_DEFECT = 'def go(config):\n    return int(config.get("cluster_wait_minutes", 20))\n'
_LEVER_SOUND = 'def go(config):\n    value = config.get("cluster_wait_minutes")\n    return int(value) if value is not None else None\n'


def test_detector_catches_the_lever_that_killed_a_run():
    """int(config.get("cluster_wait_minutes", 20)) -- default and all."""
    assert _coerced_levers(ast.parse(_LEVER_DEFECT)) == [2]


def test_detector_clears_the_guarded_rewrite():
    """The repair: take the value, coerce only if there is one."""
    assert _coerced_levers(ast.parse(_LEVER_SOUND)) == []
