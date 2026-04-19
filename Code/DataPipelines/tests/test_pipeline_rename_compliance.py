# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Verify PrescriberPipeline has been fully renamed to PrescriberEvaluateCarePipeline."""

import os
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SCAN_DIRS = [
    os.path.join(REPO_ROOT, "Code", "DataPipelines"),
    os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content"),
]
SKIP_FILES = {"conversation_log.json", "test_pipeline_rename_compliance.py"}


def _scan_for_old_name():
    """Return list of (filepath, line_num, line) containing the old name."""
    hits = []
    for scan_dir in SCAN_DIRS:
        for root, _, files in os.walk(scan_dir):
            for fname in files:
                if fname in SKIP_FILES:
                    continue
                if not fname.endswith((".py", ".json", ".yml")):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if '"PrescriberPipeline"' in line:
                                hits.append((fpath, i, line.strip()))
                except Exception:
                    pass
    return hits


def test_no_old_prescriber_pipeline_name():
    """The old task name 'PrescriberPipeline' must not appear in code or config."""
    hits = _scan_for_old_name()
    assert hits == [], f"Old name 'PrescriberPipeline' found in:\n" + "\n".join(
        f"  {fp}:{ln}: {text}" for fp, ln, text in hits
    )


def test_no_overnight_pipeline_file():
    """The file overnight_pipeline.py must not exist — renamed to prescriber_evaluate_care_pipeline.py."""
    old_path = os.path.join(REPO_ROOT, "Code", "DataPipelines", "overnight_pipeline.py")
    assert not os.path.exists(old_path), "overnight_pipeline.py still exists — must be renamed"
    new_path = os.path.join(REPO_ROOT, "Code", "DataPipelines", "prescriber_evaluate_care_pipeline.py")
    assert os.path.exists(new_path), "prescriber_evaluate_care_pipeline.py not found"


def test_new_name_in_function_app():
    """The new task name must be registered in function_app.py."""
    fpath = os.path.join(REPO_ROOT, "Code", "DataPipelines", "function_app.py")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    assert '"PrescriberEvaluateCarePipeline"' in content, \
        "New name 'PrescriberEvaluateCarePipeline' not found in function_app.py"
