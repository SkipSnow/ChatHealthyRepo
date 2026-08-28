"""The entitlement report must survive the environment it is deployed into.

Every failure this suite covers was found by deploying to Azure and reading a
job traceback, one round trip at a time: a preamble placed ahead of a
`from __future__` import, a repository root that does not exist in a sandbox,
a module imported from the repository that is not shipped with the runbook,
and characters the upload cannot carry. Each was discoverable on the machine
that produced the artefact.

These tests do that discovery here. They exercise the built artefact, not the
source, because what runs in Azure is the file the build writes.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
REPORT = ROOT / "architecture" / "IdentityCertificatesAndAuthentications" / "entitlement_report.py"
BUILD_CHAIN = ROOT / "architecture" / "DevOpsBuildDeployAndEnvironmentManagement" / "_build_chain.py"


def _inliners():
    """The build-time rewriter, loaded without importing the whole chain."""
    src = BUILD_CHAIN.read_text(encoding="utf-8")
    start = src.index("def _inline_chathealthy_lib_if_used")
    end = src.index("def _stage_runbook") if "def _stage_runbook" in src else len(src)
    ns: dict = {"Path": Path, "sys": sys, "json": json,
                "_step": lambda *a, **k: None, "step": lambda *a, **k: None}
    # The inliners reference module-level constants and the insert-point
    # helper; pull those in so the functions run as the build runs them.
    gap = chr(10) * 3
    for marker in ("_INLINE_LIB_MODULES", "def _preamble_insert_point"):
        i = src.index(marker)
        j = src.index(gap, i)
        exec(compile(src[i:j], "<inliner-deps>", "exec"), ns)     # noqa: S102
    exec(compile(src[start:end], "<inliners>", "exec"), ns)      # noqa: S102
    return ns


def _stage(tmp_path: Path) -> Path:
    dst = tmp_path / "runbook.py"
    dst.write_text(REPORT.read_text(encoding="utf-8"), encoding="utf-8")
    ns = _inliners()
    ns["_inline_chathealthy_lib_if_used"](ROOT, dst)
    return dst


def test_the_report_source_compiles():
    compile(REPORT.read_text(encoding="utf-8"), str(REPORT), "exec")


def test_the_staged_runbook_compiles(tmp_path):
    """The failure that cost a whole deploy cycle: a preamble ahead of
    `from __future__`, which Python requires to come first."""
    staged = _stage(tmp_path)
    compile(staged.read_text(encoding="utf-8"), str(staged), "exec")


def test_the_staged_runbook_carries_only_latin1(tmp_path):
    """Runbook content is uploaded through `az rest`, which cannot carry
    characters outside latin-1. Three em dashes failed an upload."""
    staged = _stage(tmp_path)
    text = staged.read_text(encoding="utf-8")
    offenders = sorted({ch for ch in text if ord(ch) > 255})
    assert not offenders, f"characters az rest cannot upload: {offenders}"


def test_the_staged_runbook_carries_no_manifest_descriptions(tmp_path):
    """The report describes what the vault and the directory hold, not what
    deployment_architecture.json says they hold. The build used to read
    secret_descriptions out of the manifest and bake them into this runbook,
    which let the report describe a secret using the file it audits. Nothing
    from the manifest may ride along."""
    staged = _stage(tmp_path)
    text = staged.read_text(encoding="utf-8")
    assert "CHATHEALTHY_SECRET_DESCRIPTIONS" not in text
    assert "inlined secret descriptions" not in text


def test_the_report_reads_identity_descriptions_from_the_directory():
    """An identity's description is a directory fact. Reading it from the
    approved register would let the register describe an identity as the
    manifest wishes it were."""
    src = REPORT.read_text(encoding="utf-8")
    assert 'e.get("description"' not in src, (
        "the identity register is supplying descriptions again")
    assert "appOwnerOrganizationId,description" in src, (
        "the directory query no longer selects description")
