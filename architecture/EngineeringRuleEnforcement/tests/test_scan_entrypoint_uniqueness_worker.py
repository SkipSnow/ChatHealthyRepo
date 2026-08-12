"""Rule-065-ENF-008 — exactly three build, deploy and promote entry points.

This enforcement runs on every commit and had no test.

Each test proves a violation is REPORTED or that a compliant file is NOT.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import scan_entrypoint_uniqueness_worker as seu
from enforcement_worker import EXIT_OK, EXIT_VIOLATIONS_FOUND

DEPLOY_DIR = "architecture/DevOpsBuildDeployAndEnvironmentManagement"

WITH_MAIN = '''\
"""A helper that declares itself an entry point."""


def main():
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

IMPORTED_ONLY = '''\
"""An imported-only helper: no entry-point block."""


def helper():
    return 0
'''


def _worker():
    w = seu.ScanEntrypointUniquenessWorker("Rule-065-ENF-008")
    w.hook = "pre-commit"
    w.scopes = []
    w.emitted = []
    w._emit_violation = w.emitted.append          # type: ignore[assignment]
    return w


def _run(worker, files: dict[str, str]):
    """Place files at their repo-relative paths and hand over the list."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        for rel, src in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src, encoding="utf-8")
        worker._files = list(files)
        with patch.object(seu.ScanEntrypointUniquenessWorker, "_repo_root",
                          lambda self: root):
            rc = worker.run()
    return rc, worker.emitted


@pytest.mark.parametrize("forbidden", sorted(seu._FORBIDDEN))
def test_forbidden_entry_point_name_is_reported(forbidden):
    w = _worker()
    rc, hits = _run(w, {f"{DEPLOY_DIR}/{forbidden}": IMPORTED_ONLY})
    assert rc == EXIT_VIOLATIONS_FOUND, f"{forbidden} must be caught by name"
    assert any(forbidden in v.message for v in hits), \
        f"the report must name the file; got {[v.message for v in hits]}"


def test_extra_entry_point_block_is_reported():
    w = _worker()
    rc, hits = _run(w, {f"{DEPLOY_DIR}/new_helper.py": WITH_MAIN})
    assert rc == EXIT_VIOLATIONS_FOUND, \
        "a fourth entry-point block in the chain directory must be caught"
    assert hits


@pytest.mark.parametrize("allowed", sorted(seu._ALLOWED_ENTRY))
def test_the_sanctioned_entry_points_pass(allowed):
    w = _worker()
    rc, hits = _run(w, {f"{DEPLOY_DIR}/{allowed}": WITH_MAIN})
    assert rc == EXIT_OK, f"{allowed} is sanctioned; got {[v.message for v in hits]}"
    assert hits == []


def test_imported_only_helper_passes():
    w = _worker()
    rc, hits = _run(w, {f"{DEPLOY_DIR}/helpers.py": IMPORTED_ONLY})
    assert rc == EXIT_OK, f"an imported-only helper must pass; got {[v.message for v in hits]}"
    assert hits == []


def test_entry_point_outside_the_chain_directory_is_ignored():
    """The rule governs one directory. A main block elsewhere is not its business."""
    w = _worker()
    rc, hits = _run(w, {"pipeline/Code/control_runner.py": WITH_MAIN})
    assert rc == EXIT_OK
    assert hits == []
