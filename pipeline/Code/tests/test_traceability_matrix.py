# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pytest: Traceability matrix completeness — v4-007, v4-023
# Verifies that the traceability matrix is valid, complete, and that
# every v4 requirement in engineering_rules.json has a corresponding entry.
#
# Run: pytest Code/DataPipelines/tests/test_traceability_matrix.py -v

import json
import os

import pytest

BRAIN_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "brain",
                 "machine_artifacts", "content")
)
MATRIX_PATH = os.path.join(BRAIN_DIR, "traceability_matrix.json")
OPERATING_RULES_PATH = os.path.join(BRAIN_DIR, "engineering_rules.json")
TESTS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__)))


def _load_json(path):
    """Load and return parsed JSON from *path*."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------------
# test_matrix_is_valid_json
# ------------------------------------------------------------------
def test_matrix_is_valid_json():
    """Verify that traceability_matrix.json parses as valid JSON."""
    data = _load_json(MATRIX_PATH)
    assert isinstance(data, dict), "Top-level value must be a JSON object"
    assert "entries" in data, "Matrix must contain an 'entries' key"
    assert isinstance(data["entries"], dict), "'entries' must be a JSON object"


# ------------------------------------------------------------------
# test_all_requirements_have_tests
# ------------------------------------------------------------------
def test_all_requirements_have_tests():
    """Every entry in the matrix must have at least one test artifact."""
    matrix = _load_json(MATRIX_PATH)
    missing = []
    for req_id, entry in matrix["entries"].items():
        artifacts = entry.get("test_artifacts", [])
        if not artifacts:
            missing.append(req_id)
    assert not missing, (
        f"Requirements with no test_artifacts: {missing}"
    )


# ------------------------------------------------------------------
# test_test_files_exist
# ------------------------------------------------------------------
def test_test_files_exist():
    """Every test file referenced in the matrix must exist on disk."""
    matrix = _load_json(MATRIX_PATH)
    missing = []
    for req_id, entry in matrix["entries"].items():
        for artifact in entry.get("test_artifacts", []):
            path = os.path.join(TESTS_DIR, artifact)
            if not os.path.isfile(path):
                missing.append((req_id, artifact))
    if missing:
        detail = "; ".join(f"{r} -> {f}" for r, f in missing)
        pytest.fail(f"Test files not found on disk: {detail}")


# ------------------------------------------------------------------
# test_no_orphan_requirements
# ------------------------------------------------------------------
def test_no_orphan_requirements():
    """Every Rule-* requirement in engineering_rules.json must appear in
    the traceability matrix. Accepts both new shape (rules.rule[]) and
    legacy shape (rules[]) for transition safety."""
    rules = _load_json(OPERATING_RULES_PATH)
    matrix = _load_json(MATRIX_PATH)
    matrix_ids = set(matrix["entries"].keys())

    rules_blob = rules.get("rules", [])
    if isinstance(rules_blob, dict):
        rule_list = rules_blob.get("rule", [])
    else:
        rule_list = rules_blob

    orphans = []
    for rule in rule_list:
        rule_id = rule.get("id", "")
        if rule_id.startswith("Rule-") and rule_id not in matrix_ids:
            orphans.append(rule_id)
    assert not orphans, (
        f"Rule-* requirements in engineering_rules but missing from "
        f"traceability matrix: {orphans}"
    )
