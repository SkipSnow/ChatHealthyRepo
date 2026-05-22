# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Pipeline orchestrator structural tests.

EPIC-010-F-101-S-001-REQ-B-003: every pipeline orchestrator MUST delete its
reservation on success AND failure (try/finally or equivalent).

EPIC-010-F-102-S-007-REQ-B-003 (V3): orchestrations are 100% independent
except they share a base class. After the base-class refactor, the shared
try/release scaffolding lives in BasePipelineOrchestrator.run() — the
Provider and Specialty subclasses override _pipeline_steps() only.
"""

import ast
import os
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
PIPELINE_DIR = os.path.join(REPO, "pipeline", "Code")
sys.path.insert(0, PIPELINE_DIR)


def _source(filename: str) -> str:
    with open(os.path.join(PIPELINE_DIR, filename), "r", encoding="utf-8") as f:
        return f.read()


def _find_def(tree: ast.AST, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_base_orchestrator_has_try_with_release_after():
    """BasePipelineOrchestrator.run() must wrap pipeline steps in try/except
    with release_reservation_activity called unconditionally AFTER the block."""
    source = _source("base_pipeline_orchestrator.py")
    tree = ast.parse(source)

    run_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            run_fn = node
            break

    assert run_fn is not None, "BasePipelineOrchestrator.run not found"
    has_try = any(
        isinstance(n, ast.Try) and (n.finalbody or n.handlers)
        for n in ast.walk(run_fn)
    )
    assert has_try, "BasePipelineOrchestrator.run must wrap pipeline steps in try/except"
    assert "release_reservation_activity" in source, (
        "BasePipelineOrchestrator.run must call release_reservation_activity"
    )


def test_provider_subclass_extends_base():
    """ProviderPipelineOrchestrator must inherit from BasePipelineOrchestrator
    and implement _pipeline_steps."""
    source = _source("provider_load_manager.py")
    tree = ast.parse(source)

    cls = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ProviderPipelineOrchestrator":
            cls = node
            break
    assert cls is not None, "ProviderPipelineOrchestrator class not found"
    base_names = [b.id for b in cls.bases if isinstance(b, ast.Name)]
    assert "BasePipelineOrchestrator" in base_names, (
        "ProviderPipelineOrchestrator must inherit BasePipelineOrchestrator"
    )
    method_names = [m.name for m in cls.body if isinstance(m, ast.FunctionDef)]
    assert "_pipeline_steps" in method_names, "_pipeline_steps must be defined"


def test_specialty_subclass_extends_base():
    """SpecialtyPipelineOrchestrator must inherit from BasePipelineOrchestrator
    and implement _pipeline_steps."""
    source = _source("load_specialty_data.py")
    tree = ast.parse(source)

    cls = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SpecialtyPipelineOrchestrator":
            cls = node
            break
    assert cls is not None, "SpecialtyPipelineOrchestrator class not found"
    base_names = [b.id for b in cls.bases if isinstance(b, ast.Name)]
    assert "BasePipelineOrchestrator" in base_names, (
        "SpecialtyPipelineOrchestrator must inherit BasePipelineOrchestrator"
    )
    method_names = [m.name for m in cls.body if isinstance(m, ast.FunctionDef)]
    assert "_pipeline_steps" in method_names, "_pipeline_steps must be defined"
