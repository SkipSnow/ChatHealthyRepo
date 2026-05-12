# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""test_workflow_generator.py — guard the four FindCare deploy YAMLs.

Verification gate for the workflow generator. The test:

  1. Constructs WorkflowGenerator with the canonical TECHNICAL_MODEL.
  2. Renders all four in-scope workflows in memory.
  3. Asserts each rendered file is byte-identical to the matching file in
     .github/workflows/ on disk.

The test fails (loudly) the moment anyone hand-edits one of the four
workflow YAMLs without regenerating from the model. That is intentional:
the four files are now defined by the generator; the on-disk copies are
artifacts of running it.

Bug history that motivates this test (do not weaken it):
  - 2026-05-09: `RemoteDeploy.WEBSITE_WORKFLOWS["prod"]` was pointing at
    the QA yaml. Prod website deploys appeared green in `gh workflow run`
    output but actually replayed the QA workflow. Found only because Skip
    happened to look at the file. The generator + this test prevent that
    class of silent mis-mapping from being possible.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from Code.deploy.workflow_generator.technical_model import TECHNICAL_MODEL
from Code.deploy.workflow_generator.workflow_generator import WorkflowGenerator


def _repo_root() -> Path:
    if "CHATHEALTHY_PROJECT_ROOT" in os.environ:
        return Path(os.environ["CHATHEALTHY_PROJECT_ROOT"]).resolve()
    # Fall back to walking up from this file: workflow_generator/ -> deploy/
    # -> Code/ -> repo root.
    return Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def generator() -> WorkflowGenerator:
    return WorkflowGenerator(TECHNICAL_MODEL, repo_root=_repo_root())


def test_render_all_returns_four_in_scope_files(generator: WorkflowGenerator) -> None:
    """The generator owns exactly the four FindCare-related workflows."""
    rendered = generator.render_all()
    assert set(rendered.keys()) == set(WorkflowGenerator.IN_SCOPE_FILENAMES)


def test_rendered_yaml_parses(generator: WorkflowGenerator) -> None:
    """Each rendered file must be syntactically valid YAML.

    `render_all` already runs yaml.safe_load internally and raises on
    failure; this test simply asserts the call succeeds end-to-end.
    """
    rendered = generator.render_all()
    assert len(rendered) == 4
    for fn, content in rendered.items():
        assert content.startswith("name: "), (
            f"{fn} should start with a 'name:' field but starts with: "
            f"{content[:60]!r}"
        )


@pytest.mark.parametrize(
    "filename", WorkflowGenerator.IN_SCOPE_FILENAMES
)
def test_on_disk_matches_generator_output(
    generator: WorkflowGenerator, filename: str
) -> None:
    """Each on-disk YAML must match the generator output byte-for-byte.

    If this fails, EITHER:
      - someone hand-edited .github/workflows/<filename> (forbidden — the
        file is generator-owned now). Regenerate via:
            python -m Code.deploy.workflow_generator
      - OR the technical_model / generator was changed and the on-disk
        YAML was not regenerated. Regenerate via the same command.
    """
    repo_root = _repo_root()
    target = repo_root / ".github" / "workflows" / filename
    assert target.exists(), f"workflow file missing on disk: {target}"

    rendered = generator.render_all()
    rendered_bytes = WorkflowGenerator._encode(rendered[filename])
    on_disk_bytes = target.read_bytes()

    if rendered_bytes == on_disk_bytes:
        return

    # Produce an actionable diff message on failure. Normalize line
    # endings for the human-readable diff (both sides become LF) so the
    # output reflects content drift, not whitespace.
    import difflib
    on_disk_text = on_disk_bytes.decode("utf-8").replace("\r\n", "\n")
    rendered_text = rendered[filename]
    diff = "\n".join(
        difflib.unified_diff(
            on_disk_text.splitlines(),
            rendered_text.splitlines(),
            fromfile=f"on-disk/{filename}",
            tofile=f"generator/{filename}",
            lineterm="",
        )
    )
    pytest.fail(
        f"{filename} on disk diverges from generator output.\n"
        f"on-disk size={len(on_disk_bytes)} bytes; "
        f"generator size={len(rendered_bytes)} bytes\n"
        f"--- unified diff (LF-normalized) ---\n{diff}"
    )
