# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""The data gate and the steps agree on the dependency vocabulary.

The check holds in BOTH directions, so neither side can drift again: a kind
the gate resolves but no step names is a dead branch, and a kind a step names
but the gate cannot resolve is a step that will never run.
"""

from __future__ import annotations

import ast
import pathlib

_ORCHESTRATOR = (pathlib.Path(__file__).resolve().parents[2]
                 / "medicare_orchestrator.py")

def _declared_kinds() -> set[str]:
    """Every kind named by a step in STEP_DATA_DEPENDENCIES."""
    from pipeline.MedicareProviderEvaluationDataPipeline.medicare_orchestrator import (
        MedicareProviderEvaluationOrchestrator)
    kinds: set[str] = set()
    deps_map = MedicareProviderEvaluationOrchestrator.STEP_DATA_DEPENDENCIES
    for deps in deps_map.values():
        for dep in deps:
            text = dep
            # the conditional prefixes come off first; what remains is either
            # a staging source reference or a gate kind
            for prefix in ("if_build_fda:", "if_consume_fda:"):
                if text.startswith(prefix):
                    text = text[len(prefix):]
            if text.startswith("staging:"):
                continue
            kinds.add(text)
    return kinds


def _resolved_kinds() -> set[str]:
    """Every kind the gate compares `dep` against, read from the source."""
    tree = ast.parse(_ORCHESTRATOR.read_text(encoding="utf-8"))
    kinds: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.comparators) != 1:
            continue
        left, op, right = node.left, node.ops[0], node.comparators[0]
        if not isinstance(op, ast.Eq):
            continue
        if isinstance(left, ast.Name) and left.id == "dep" and \
                isinstance(right, ast.Constant) and isinstance(right.value, str):
            kinds.add(right.value)
    return kinds


def test_every_kind_the_gate_resolves_is_named_by_a_step():
    unreachable = _resolved_kinds() - _declared_kinds()
    assert not unreachable, (
        f"the gate resolves kinds no step declares: {sorted(unreachable)}")


def test_every_kind_a_step_declares_is_resolved_by_the_gate():
    unresolvable = _declared_kinds() - _resolved_kinds()
    assert not unresolvable, (
        f"steps declare kinds the gate cannot resolve: {sorted(unresolvable)}")
