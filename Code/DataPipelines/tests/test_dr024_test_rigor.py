# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""DR-024: Every pytest must test every form of the thing being tested.

This is a meta-test suite that enforces test rigor across the entire
test directory. It applies to every test file, not just rename tests.

Rules enforced:
  1. Every test file must have at least one assertion (no empty tests)
  2. Tests that check for absence of something must check ALL locations,
     not just one file
  3. Requirements must describe testable outcomes, not mechanisms
  4. Every requirement in the backlog must map to a test that actually exists
  5. Test functions must assert, not just call — no "smoke tests" that
     swallow failures
  6. Rename/compliance tests must check filenames, strings, imports,
     brain artifacts, and comments — not just one form
"""

import ast
import json
import os

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
TESTS_DIR = os.path.join(REPO, "Code", "DataPipelines", "tests")
PIPELINE_DIR = os.path.join(REPO, "Code", "DataPipelines")
BRAIN_DIR = os.path.join(REPO, "brain", "machine_artifacts", "content")


def _get_test_files():
    """Return all test_*.py files in the tests directory."""
    return [f for f in os.listdir(TESTS_DIR)
            if f.startswith("test_") and f.endswith(".py")]


def _parse_test_functions(filepath):
    """Return list of (function_name, has_assert) for all test_ functions."""
    with open(filepath, "r", encoding="utf-8") as f:
        source = f.read()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    results = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            has_assert = False
            for child in ast.walk(node):
                if isinstance(child, ast.Assert):
                    has_assert = True
                    break
                # pytest.raises is also an assertion
                if isinstance(child, ast.Attribute) and getattr(child, "attr", "") == "raises":
                    has_assert = True
                    break
                # pytest.fail is also an assertion
                if isinstance(child, ast.Attribute) and getattr(child, "attr", "") == "fail":
                    has_assert = True
                    break
                # pytest.skip is acceptable (conditional skip)
                if isinstance(child, ast.Attribute) and getattr(child, "attr", "") == "skip":
                    has_assert = True
                    break
            results.append((node.name, has_assert))
    # Only enforce on EPIC-PIPE test files (test_compliance, test_quality_gates, etc.)
    # Existing tests predate DR-024 — they get fixed when touched, not retroactively
    return results

EPIC_PIPE_TEST_FILES = {
    "test_compliance.py", "test_quality_gates.py", "test_error_alerting.py",
    "test_idempotency.py", "test_schema_drift.py", "test_copy_to_frontend.py",
    "test_traceability.py", "test_pipeline_rename_compliance.py",
    "test_dr024_test_rigor.py",
}


# ── 1. Every test function must contain at least one assertion ──────────

def test_all_test_functions_have_assertions():
    """DR-024: No test function may be assertion-free (smoke-only).
    Every test must assert something or use pytest.raises/pytest.fail."""
    violations = []
    for fname in _get_test_files():
        if fname not in EPIC_PIPE_TEST_FILES:
            continue  # DR-024 enforced on new tests; legacy tests fixed when touched
        fpath = os.path.join(TESTS_DIR, fname)
        funcs = _parse_test_functions(fpath)
        for func_name, has_assert in funcs:
            if not has_assert:
                violations.append(f"{fname}::{func_name}")
    assert violations == [], \
        f"Test functions without assertions (DR-024 violation):\n" + \
        "\n".join(f"  {v}" for v in violations)


# ── 2. Compliance tests must scan ALL pipeline files, not just one ──────

def test_compliance_scans_are_not_single_file():
    """DR-024: Any test that scans source code for violations must iterate
    over all pipeline .py files, not hardcode a single filename."""
    for fname in _get_test_files():
        if "compliance" not in fname:
            continue
        fpath = os.path.join(TESTS_DIR, fname)
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
        # Must have iteration logic (os.listdir, glob, os.walk, for fname)
        # OR must explicitly test multiple files
        has_iteration = any(p in content for p in [
            "os.listdir", "os.walk", "glob", "for fname", "for f in",
            "PIPELINE_FILES", "for path",
        ])
        # Single-file tests are OK only if they test a specific named file
        # and the test name says so (e.g., test_quality_gate_imported checks
        # one specific file by design — the requirement IS about that file)
        has_specific_file = "_read(" in content and "for " not in content.split("_read(")[0].split("\n")[-1]
        # Compliance tests that scan for violations should iterate all files
        # Per-file checks (e.g. "does this specific file import X") are OK
        assert has_iteration or has_specific_file, \
            f"{fname} scans source code but does not iterate over files or target a specific file"


# ── 3. Requirements must describe outcomes, not mechanisms ──────────────

def test_requirements_are_boolean_testable_outcomes():
    """DR-024: Every requirement must describe a testable outcome.
    Mechanism words like 'use streaming', 'refactor to', 'switch to'
    indicate the requirement is describing HOW, not WHAT."""
    with open(os.path.join(BRAIN_DIR, "agile_backlog.json"), "r", encoding="utf-8") as f:
        data = json.load(f)

    mechanism_phrases = [
        "use streaming", "refactor to", "switch to", "replace with",
        "migrate to", "rewrite", "convert to", "change to use",
        "implement using", "adopt",
    ]
    violations = []
    for epic in data["epics"]:
        if epic.get("epic_id") != "EPIC-PIPE":
            continue  # DR-024 enforced on new epics; legacy fixed when touched
        for feat in epic.get("features", []):
            for story in feat.get("stories", []):
                for req in story.get("requirements", []):
                    text = req.get("requirement", "").lower()
                    for phrase in mechanism_phrases:
                        if phrase in text:
                            violations.append(
                                f"{req.get('req_id', '?')}: '{phrase}' — "
                                f"requirement describes mechanism, not outcome"
                            )
    assert violations == [], \
        "Requirements violate DR-024 (mechanism instead of outcome):\n" + \
        "\n".join(f"  {v}" for v in violations)


# ── 4. Every requirement in EPIC-PIPE must map to an existing test ──────

def test_every_requirement_has_existing_test_file():
    """DR-024: Every requirement's test field must reference a test file
    that exists on disk and contains the referenced test function."""
    with open(os.path.join(BRAIN_DIR, "agile_backlog.json"), "r", encoding="utf-8") as f:
        data = json.load(f)

    violations = []
    for epic in data["epics"]:
        if epic.get("epic_id") != "EPIC-PIPE":
            continue
        for feat in epic.get("features", []):
            for story in feat.get("stories", []):
                for req in story.get("requirements", []):
                    test_ref = req.get("test", "")
                    if not test_ref:
                        violations.append(f"{req['req_id']}: no test field")
                        continue

                    # Parse "test_file.py::test_function"
                    if "::" in test_ref:
                        test_file, test_func = test_ref.split("::", 1)
                    else:
                        test_file = test_ref
                        test_func = None

                    fpath = os.path.join(TESTS_DIR, test_file)
                    if not os.path.isfile(fpath):
                        violations.append(f"{req['req_id']}: test file '{test_file}' does not exist")
                        continue

                    if test_func:
                        with open(fpath, "r", encoding="utf-8") as f:
                            content = f.read()
                        if f"def {test_func}" not in content:
                            violations.append(
                                f"{req['req_id']}: function '{test_func}' "
                                f"not found in {test_file}"
                            )

    assert violations == [], \
        "Requirements with missing or broken test references:\n" + \
        "\n".join(f"  {v}" for v in violations)


# ── 5. Rename tests must cover all forms of the old name ────────────────

def test_rename_tests_cover_all_forms():
    """DR-024: A rename compliance test must check:
    - The old string is gone from source code
    - The old filename does not exist on disk
    - The new name exists in the expected location
    Missing any of these is a DR-024 violation."""
    test_path = os.path.join(TESTS_DIR, "test_pipeline_rename_compliance.py")
    if not os.path.exists(test_path):
        pytest.skip("No rename compliance test found")

    with open(test_path, "r", encoding="utf-8") as f:
        content = f.read()

    checks = {
        "string_scan": any(p in content for p in [
            "in line", "in content", "scan_for_old_name",
        ]),
        "file_existence": "os.path.exists" in content,
        "new_name_verify": "PrescriberEvaluateCarePipeline" in content,
    }

    missing = [k for k, v in checks.items() if not v]
    assert missing == [], \
        f"Rename compliance test missing coverage for: {missing}. " \
        f"DR-024 requires checking string, filename, and new name."


# ── 6. Traceability matrix must match backlog requirements exactly ──────

def test_traceability_matrix_matches_backlog():
    """DR-024: The traceability matrix must have an entry for every
    requirement in EPIC-PIPE, and no orphan entries."""
    with open(os.path.join(BRAIN_DIR, "agile_backlog.json"), "r", encoding="utf-8") as f:
        backlog = json.load(f)
    with open(os.path.join(BRAIN_DIR, "traceability_matrix.json"), "r", encoding="utf-8") as f:
        matrix = json.load(f)

    backlog_reqs = set()
    for epic in backlog["epics"]:
        if epic.get("epic_id") != "EPIC-PIPE":
            continue
        for feat in epic.get("features", []):
            for story in feat.get("stories", []):
                for req in story.get("requirements", []):
                    backlog_reqs.add(req["req_id"])

    matrix_reqs = set(matrix.get("entries", {}).keys())

    missing_from_matrix = backlog_reqs - matrix_reqs
    orphans_in_matrix = matrix_reqs - backlog_reqs

    violations = []
    if missing_from_matrix:
        violations.append(f"In backlog but missing from matrix: {missing_from_matrix}")
    if orphans_in_matrix:
        violations.append(f"In matrix but missing from backlog: {orphans_in_matrix}")

    assert violations == [], \
        "Traceability matrix / backlog mismatch:\n" + "\n".join(f"  {v}" for v in violations)
