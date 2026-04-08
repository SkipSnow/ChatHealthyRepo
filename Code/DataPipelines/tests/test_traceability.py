# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Traceability matrix tests — PIPE-TR-001"""
import json, os, pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
MATRIX_PATH = os.path.join(REPO, "brain", "machine_artifacts", "content", "traceability_matrix.json")
BACKLOG_PATH = os.path.join(REPO, "brain", "machine_artifacts", "content", "agile_backlog.json")
TESTS_DIR = os.path.join(REPO, "Code", "DataPipelines", "tests")

def _load_matrix():
    with open(MATRIX_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def test_matrix_valid_json():
    """PIPE-TR-001-REQ-003: matrix file must parse and have required structure"""
    matrix = _load_matrix()
    assert "_record_id" in matrix
    assert "entries" in matrix
    assert len(matrix["entries"]) > 0

def test_all_requirements_have_tests():
    """PIPE-TR-001-REQ-001: every requirement in EPIC-PIPE has test artifact(s)"""
    matrix = _load_matrix()
    for req_id, entry in matrix["entries"].items():
        tests = entry.get("test_artifacts", [])
        assert len(tests) > 0, f"Requirement {req_id} has no test artifacts"

def test_test_files_exist():
    """PIPE-TR-001-REQ-002: every test file in matrix exists on disk"""
    matrix = _load_matrix()
    missing = []
    for req_id, entry in matrix["entries"].items():
        for test_file in entry.get("test_artifacts", []):
            # Extract just the filename (before ::)
            fname = test_file.split("::")[0]
            fpath = os.path.join(TESTS_DIR, fname)
            if not os.path.isfile(fpath):
                missing.append(f"{req_id} -> {fname}")
    assert missing == [], f"Missing test files:\n" + "\n".join(missing)
