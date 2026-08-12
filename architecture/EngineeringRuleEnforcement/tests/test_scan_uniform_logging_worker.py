"""Rule-065-ENF-004 — logging goes through ChatHealthyLoggingService.

This enforcement runs on every commit and had no test. It is also the rule
whose 21 exclusions I added and later removed, so it has already been used
to wave work through once; it needs to be provably real.

Each test proves a violation is REPORTED or that compliant code is NOT.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import scan_uniform_logging_worker as sul
from enforcement_worker import EXIT_OK, EXIT_VIOLATIONS_FOUND

PRINT = '''\
"""Uses print."""


def announce(msg):
    print(msg)
'''

GET_LOGGER = '''\
"""Uses logging.getLogger."""
import logging

log = logging.getLogger(__name__)
'''

BASIC_CONFIG = '''\
"""Uses logging.basicConfig."""
import logging

logging.basicConfig(level=logging.INFO)
'''

BARE_FROM_IMPORT = '''\
"""Bare call after from-import."""
from logging import getLogger

log = getLogger(__name__)
'''

COMPLIANT = '''\
"""The canonical logger."""
from chathealthy_lib.logging_service import ChatHealthyLoggingService

log = ChatHealthyLoggingService()


def announce(msg):
    log.info(msg)
'''


def _worker(scopes_by_check=None):
    w = sul.ScanUniformLoggingEnforcementWorker("Rule-065-ENF-004")
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
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / name).write_text(source, encoding="utf-8")
        worker._files = [f"pkg/{name}"]
        with patch.object(sul, "PROJECT_ROOT", root):
            rc = worker.run()
    return rc, worker.emitted


@pytest.mark.parametrize("name,source,needle", [
    ("uses_print.py",      PRINT,            "print"),
    ("get_logger.py",      GET_LOGGER,       "getLogger"),
    ("basic_config.py",    BASIC_CONFIG,     "basicConfig"),
    ("bare_from.py",       BARE_FROM_IMPORT, "getLogger"),
])
def test_forbidden_call_is_reported(name, source, needle):
    w = _worker({"_scan_uniform_logging": {"allowed_pattern": [r"\.py$"]}})
    rc, hits = _run(w, name, source)
    assert rc == EXIT_VIOLATIONS_FOUND, f"{needle} must be caught"
    assert hits, "returned the violation code but reported nothing"
    assert any(needle in v.message for v in hits), \
        f"the report must name {needle}; got {[v.message for v in hits]}"


def test_canonical_logger_passes():
    w = _worker({"_scan_uniform_logging": {"allowed_pattern": [r"\.py$"]}})
    rc, hits = _run(w, "compliant.py", COMPLIANT)
    assert rc == EXIT_OK, f"the canonical logger must pass; got {[v.message for v in hits]}"
    assert hits == []


def test_excluded_file_is_not_reported():
    w = _worker({"_scan_uniform_logging": {
        "allowed_pattern": [r"\.py$"],
        "excluded_exact": ["pkg/uses_print.py"],
    }})
    rc, hits = _run(w, "uses_print.py", PRINT)
    assert rc == EXIT_OK
    assert hits == []
