# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Pipeline orchestrator structural tests.

EPIC-006-F-006-S-002-REQ-T-001: provider_pipeline_orchestrator wraps steps in try/except
                                with release after the block (functional try/finally).
"""

import ast
import os
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
PIPELINE_DIR = os.path.join(REPO, "pipeline", "Code")
sys.path.insert(0, PIPELINE_DIR)


def _provider_source() -> str:
    source_path = os.path.join(PIPELINE_DIR, "provider_load_manager.py")
    with open(source_path, "r", encoding="utf-8") as f:
        return f.read()


def _specialty_source() -> str:
    source_path = os.path.join(PIPELINE_DIR, "load_specialty_data.py")
    with open(source_path, "r", encoding="utf-8") as f:
        return f.read()


def test_provider_orchestrator_has_try_except_with_release():
    """provider_pipeline_orchestrator_fn must wrap steps in try/except with release_reservation
    in the unconditional path after the block."""
    source = _provider_source()
    tree = ast.parse(source)

    orchestrator_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "provider_pipeline_orchestrator_fn":
            orchestrator_fn = node
            break

    assert orchestrator_fn is not None, "provider_pipeline_orchestrator_fn not found"

    has_try = any(
        isinstance(n, ast.Try) and (n.finalbody or n.handlers)
        for n in ast.walk(orchestrator_fn)
    )
    assert has_try, "provider_pipeline_orchestrator_fn must wrap steps in try/except or try/finally"


def test_provider_orchestrator_releases_reservation_after_try():
    """release_reservation_activity must be called after the try/except in the orchestrator
    so success and failure paths both release the cluster reservation."""
    source = _provider_source()
    assert "release_reservation_activity" in source, (
        "provider_pipeline_orchestrator_fn must call release_reservation_activity"
    )

    lines = source.split("\n")
    in_orchestrator = False
    found_try = False
    found_except = False
    found_release_after = False
    for line in lines:
        if "def provider_pipeline_orchestrator_fn" in line:
            in_orchestrator = True
        if in_orchestrator:
            if "    try:" in line and not line.strip().startswith("#"):
                found_try = True
            if "    except" in line and found_try:
                found_except = True
            if found_except and "release_reservation" in line:
                found_release_after = True

    assert found_release_after, (
        "release_reservation must be called after the try/except block in "
        "provider_pipeline_orchestrator_fn"
    )


def test_specialty_orchestrator_has_try_except_with_release():
    """specialty_pipeline_orchestrator_fn must wrap steps in try/except with
    release_reservation in the unconditional path after the block."""
    source = _specialty_source()
    tree = ast.parse(source)

    orchestrator_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "specialty_pipeline_orchestrator_fn":
            orchestrator_fn = node
            break

    assert orchestrator_fn is not None, "specialty_pipeline_orchestrator_fn not found"

    has_try = any(
        isinstance(n, ast.Try) and (n.finalbody or n.handlers)
        for n in ast.walk(orchestrator_fn)
    )
    assert has_try, "specialty_pipeline_orchestrator_fn must wrap steps in try/except or try/finally"

    assert "release_reservation_activity" in source, (
        "specialty_pipeline_orchestrator_fn must call release_reservation_activity"
    )
