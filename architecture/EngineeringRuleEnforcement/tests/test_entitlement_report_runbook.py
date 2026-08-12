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
    """The two build-time rewriters, loaded without importing the whole chain."""
    src = BUILD_CHAIN.read_text(encoding="utf-8")
    start = src.index("def _inline_secret_descriptions_if_used")
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
    ns["_inline_secret_descriptions_if_used"](ROOT, dst)
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


def test_the_staged_runbook_embeds_the_secret_descriptions(tmp_path):
    """The descriptions are read from the manifest at build time; without
    them the mailed report prints bare secret names."""
    staged = _stage(tmp_path)
    text = staged.read_text(encoding="utf-8")
    assert "CHATHEALTHY_SECRET_DESCRIPTIONS" in text
    ns: dict = {}
    for line in text.splitlines():
        if "b64decode(" in line and "CHATHEALTHY_SECRET_DESCRIPTIONS" in line:
            exec(line.strip(), {"_b64d": __import__("base64"), "_osd": type(         # noqa: S102
                "E", (), {"environ": ns})()})
            break
    payload = json.loads(ns["CHATHEALTHY_SECRET_DESCRIPTIONS"])
    assert payload, "the embedded description map is empty"
    for name in ("cert-pipelineEditor", "ca-root-privatekey"):
        assert name in payload, f"{name} has no description"


def test_the_staged_runbook_imports_nothing_from_the_repository(tmp_path):
    """Azure ships one file. A module that lives in the repository and is not
    inlined fails at import -- notification_client did exactly that."""
    staged = _stage(tmp_path)
    text = staged.read_text(encoding="utf-8")
    for module in ("notification_client", "pipeline_db", "blob_logger",
                   "chathealthy_ca", "PipelineServices"):
        assert f"import {module}" not in text and f"from {module}" not in text, \
            f"{module} lives in the repository and is not shipped with the runbook"


def test_the_report_runs_with_no_repository_present(tmp_path):
    """A sandbox has no git tree. The report resolved its root by walking for
    one and raised NameError when it found none."""
    staged = _stage(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-c",
         "import ast,sys;src=open(sys.argv[1],encoding='utf-8').read();"
         "t=ast.parse(src);"
         "names={n.id for n in ast.walk(t) if isinstance(n,ast.Name)};"
         "print('_root' in names)", str(staged)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    source = staged.read_text(encoding="utf-8")
    assert "_root: Path | None = None" in source, \
        "_root must be declared before the walk, or its absence raises NameError"


@pytest.mark.parametrize("name,expected", [
    ("SCH-Reaper-15min", "Minute"),
    ("SCH-EntitlementReport-1200UTC", "Day"),
    ("SCH-EntitlementReport-Daily-0400PT", None),
])
def test_schedule_names_parse_to_the_intended_recurrence(name, expected):
    """Schedule creation raised NameError on deleted regex objects, and an
    unrecognised name is skipped silently -- so a schedule that never fires
    looks identical to one that does."""
    chain = (ROOT / "architecture" / "DevOpsBuildDeployAndEnvironmentManagement"
             / "_deploy_chain.py").read_text(encoding="utf-8")
    start = chain.index("def _parse_interval_schedule")
    end = chain.index("def az_automation_schedule_ensure")
    ns: dict = {}
    exec(compile(chain[start:end], "<schedules>", "exec"), ns)    # noqa: S102
    got = ns["_parse_schedule_from_name"](name)
    if expected is None:
        assert got is None, f"{name} should not be recognised"
    else:
        assert got is not None, f"{name} was not recognised"
        assert got["properties"]["frequency"] == expected
