"""Rule-065-ENF-003 — Mongo access only through the canonical utility.

This enforcement runs on every commit and had no test. It is the rule that
keeps a certificate identity on every connection, so a hole in it is a hole
in the authorization model, not a style lapse.

Each test proves a violation is REPORTED or that compliant code is NOT.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import scan_mongo_client_direct_access_worker as smc
from enforcement_worker import EXIT_OK, EXIT_VIOLATIONS_FOUND

BARE = '''\
"""Direct construction."""
from pymongo import MongoClient


def connect(uri):
    return MongoClient(uri)
'''

QUALIFIED = '''\
"""Qualified construction."""
import pymongo


def connect(uri):
    return pymongo.MongoClient(uri)
'''

COMPLIANT = '''\
"""The canonical path."""
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities


def connect():
    return ChatHealthyMongoUtilities().getConnection("pipelineEditor", "admin")
'''


def _worker(scopes_by_check=None):
    w = smc.ScanMongoClientDirectAccessEnforcementWorker("Rule-065-ENF-003")
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
        with patch.object(smc, "PROJECT_ROOT", root):
            rc = worker.run()
    return rc, worker.emitted


def test_bare_mongoclient_is_reported():
    w = _worker({"_scan_mongo_client_direct_access": {"allowed_pattern": [r"\.py$"]}})
    rc, hits = _run(w, "bare.py", BARE)
    assert rc == EXIT_VIOLATIONS_FOUND, "MongoClient(uri) must be caught"
    assert hits, "returned the violation code but reported nothing"


def test_qualified_mongoclient_is_reported():
    w = _worker({"_scan_mongo_client_direct_access": {"allowed_pattern": [r"\.py$"]}})
    rc, hits = _run(w, "qualified.py", QUALIFIED)
    assert rc == EXIT_VIOLATIONS_FOUND, "pymongo.MongoClient(uri) must be caught"
    assert hits


@pytest.mark.parametrize("module", smc._FAKE_MODULES)
def test_every_fake_module_is_reported(module):
    """No test-file exemption: a double is an unauthenticated client."""
    src = f'"""A fake."""\nimport {module}\n\n\ndef connect():\n    return {module}.MongoClient()\n'
    w = _worker({"_scan_mongo_client_direct_access": {"allowed_pattern": [r"\.py$"]}})
    rc, hits = _run(w, "fake.py", src)
    assert rc == EXIT_VIOLATIONS_FOUND, f"{module} must be caught"
    assert hits


def test_canonical_utility_passes():
    w = _worker({"_scan_mongo_client_direct_access": {"allowed_pattern": [r"\.py$"]}})
    rc, hits = _run(w, "compliant.py", COMPLIANT)
    assert rc == EXIT_OK, f"getConnection must pass; got {[v.message for v in hits]}"
    assert hits == []


def test_excluded_file_is_not_reported():
    w = _worker({"_scan_mongo_client_direct_access": {
        "allowed_pattern": [r"\.py$"],
        "excluded_exact": ["pkg/bare.py"],
    }})
    rc, hits = _run(w, "bare.py", BARE)
    assert rc == EXIT_OK
    assert hits == []
