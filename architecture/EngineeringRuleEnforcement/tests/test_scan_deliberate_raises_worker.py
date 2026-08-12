"""Rule-065-ENF-002 — deliberate raises use ChatHealthyException.

This enforcement runs on every commit and had no test. A gate nobody has
proved catches anything is a gate that reports clean because it looked at
nothing, which is worse than no gate at all.

Each test here proves one of two things and nothing weaker: that a file
which violates is REPORTED, or that a file which complies is NOT. An
assertion that the worker merely ran is not evidence.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import scan_deliberate_raises_enforcement_worker as sdr
from enforcement_worker import EXIT_OK, EXIT_VIOLATIONS_FOUND

VIOLATING = '''\
"""A module that raises a forbidden built-in."""


def check(value):
    if value < 0:
        raise ValueError("negative")
    return value
'''

COMPLIANT = '''\
"""A module that raises the canonical exception."""
from chathealthy_lib.exceptions import ChatHealthyException


def check(value):
    if value < 0:
        raise ChatHealthyException(
            mode="value_error", component="fixture", message="negative")
    return value
'''

BARE_RERAISE = '''\
"""A bare re-raise is always allowed."""


def check(fn):
    try:
        return fn()
    except Exception:
        raise
'''


def _worker(scopes_by_check=None):
    w = sdr.ScanDeliberateRaisesEnforcementWorker("Rule-065-ENF-002")
    w.hook = "pre-commit"
    triples = []
    for check, kinds in (scopes_by_check or {}).items():
        for kind, terms in kinds.items():
            triples.append((check, kind, list(terms)))
    w.scopes = triples
    w.emitted = []
    w._emit_violation = w.emitted.append          # type: ignore[assignment]
    return w


def _run(worker, name: str, source: str):
    """Place one file and hand the worker the list, as the driver does."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / name).write_text(source, encoding="utf-8")
        worker._files = [f"pkg/{name}"]
        with patch.object(sdr, "PROJECT_ROOT", root):
            rc = worker.run()
    return rc, worker.emitted


def test_forbidden_builtin_raise_is_reported():
    w = _worker({"_scan_deliberate_raises": {"allowed_pattern": [r"\.py$"]}})
    rc, hits = _run(w, "violating.py", VIOLATING)
    assert rc == EXIT_VIOLATIONS_FOUND, "a raise ValueError must be caught"
    assert hits, "the worker returned the violation code but reported nothing"
    assert any("ValueError" in v.message for v in hits), \
        f"the report must name the forbidden type; got {[v.message for v in hits]}"


@pytest.mark.parametrize("builtin", ["RuntimeError", "AssertionError",
                                     "KeyError", "TypeError"])
def test_every_forbidden_builtin_is_reported(builtin):
    src = VIOLATING.replace("ValueError", builtin)
    w = _worker({"_scan_deliberate_raises": {"allowed_pattern": [r"\.py$"]}})
    rc, hits = _run(w, "violating.py", src)
    assert rc == EXIT_VIOLATIONS_FOUND, f"raise {builtin} must be caught"
    assert any(builtin in v.message for v in hits)


def test_chathealthy_exception_passes():
    w = _worker({"_scan_deliberate_raises": {"allowed_pattern": [r"\.py$"]}})
    rc, hits = _run(w, "compliant.py", COMPLIANT)
    assert rc == EXIT_OK, f"the canonical raise must pass; got {[v.message for v in hits]}"
    assert hits == []


def test_bare_reraise_passes():
    w = _worker({"_scan_deliberate_raises": {"allowed_pattern": [r"\.py$"]}})
    rc, hits = _run(w, "reraise.py", BARE_RERAISE)
    assert rc == EXIT_OK, f"a bare re-raise is allowed; got {[v.message for v in hits]}"
    assert hits == []


def test_excluded_file_is_not_reported():
    """The exclusion mechanism must actually suppress, or every exclusion
    granted on the strength of these tests rests on nothing."""
    w = _worker({"_scan_deliberate_raises": {
        "allowed_pattern": [r"\.py$"],
        "excluded_exact": ["pkg/violating.py"],
    }})
    rc, hits = _run(w, "violating.py", VIOLATING)
    assert rc == EXIT_OK
    assert hits == []


def test_file_outside_scope_is_not_read():
    w = _worker({"_scan_deliberate_raises": {"allowed_pattern": [r"\.md$"]}})
    rc, hits = _run(w, "violating.py", VIOLATING)
    assert rc == EXIT_OK
    assert hits == []
