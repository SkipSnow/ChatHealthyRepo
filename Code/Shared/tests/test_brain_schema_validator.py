# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# EPIC-008-F-007-S-020-REQ-T-001: Schema enforcement tests.
#
# Every constrained field in schema.json must reject illegal values.
# These tests hand illegal values to the validator and verify they fail.

import copy
import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from brain_schema_validator import (
    validate_backlog,
    validate_bugs,
    validate_all,
    _load_schema,
    _find_collection_schema,
    _extract_constraints,
    _validate_record,
)

BRAIN_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "brain")


class TestCurrentFilesAreValid:
    """Baseline: current brain files must pass validation."""

    def test_backlog_is_valid(self):
        errors = validate_backlog(BRAIN_DIR)
        assert errors == [], f"agile_backlog.json has violations: {errors}"

    def test_bugs_is_valid(self):
        errors = validate_bugs(BRAIN_DIR)
        assert errors == [], f"bugs.json has violations: {errors}"

    def test_validate_all_passes(self):
        results = validate_all(BRAIN_DIR)
        for collection, errors in results.items():
            assert errors == [], f"{collection} has violations: {errors}"


class TestIllegalEpicStatusRejected:
    """Epic status must be constrained by CV-005."""

    def test_building_rejected(self):
        errors = _validate_record(
            {"status": "building"},
            "epic",
            {"epic": {"status": ["not_started", "in_progress", "done", "done_known_issue", "to_test", "blocked", "deferred"]}},
            "test_epic",
        )
        assert len(errors) == 1
        assert "building" in errors[0]

    def test_active_rejected(self):
        errors = _validate_record(
            {"status": "active"},
            "epic",
            {"epic": {"status": ["not_started", "in_progress", "done", "done_known_issue", "to_test", "blocked", "deferred"]}},
            "test_epic",
        )
        assert len(errors) == 1

    def test_in_progress_accepted(self):
        errors = _validate_record(
            {"status": "in_progress"},
            "epic",
            {"epic": {"status": ["not_started", "in_progress", "done", "done_known_issue", "to_test", "blocked", "deferred"]}},
            "test_epic",
        )
        assert errors == []


class TestIllegalFeatureStatusRejected:
    """Feature status must be constrained by CV-005."""

    def test_building_rejected(self):
        schema = _load_schema(BRAIN_DIR)
        col_schema = _find_collection_schema(schema, "agile_backlog")
        constraints = _extract_constraints(col_schema.get("record_schemas", {}))
        errors = _validate_record({"status": "building"}, "feature", constraints, "test_feature")
        assert len(errors) == 1
        assert "building" in errors[0]

    def test_in_progress_accepted(self):
        schema = _load_schema(BRAIN_DIR)
        col_schema = _find_collection_schema(schema, "agile_backlog")
        constraints = _extract_constraints(col_schema.get("record_schemas", {}))
        errors = _validate_record({"status": "in_progress"}, "feature", constraints, "test_feature")
        assert errors == []


class TestIllegalStoryStatusRejected:
    """Story status must be constrained by CV-005."""

    def test_completed_rejected(self):
        schema = _load_schema(BRAIN_DIR)
        col_schema = _find_collection_schema(schema, "agile_backlog")
        constraints = _extract_constraints(col_schema.get("record_schemas", {}))
        errors = _validate_record({"status": "completed"}, "story", constraints, "test_story")
        assert len(errors) == 1

    def test_done_accepted(self):
        schema = _load_schema(BRAIN_DIR)
        col_schema = _find_collection_schema(schema, "agile_backlog")
        constraints = _extract_constraints(col_schema.get("record_schemas", {}))
        errors = _validate_record({"status": "done"}, "story", constraints, "test_story")
        assert errors == []


class TestIllegalRequirementStatusRejected:
    """Requirement status must be constrained by CV-005."""

    def test_finished_rejected(self):
        schema = _load_schema(BRAIN_DIR)
        col_schema = _find_collection_schema(schema, "agile_backlog")
        constraints = _extract_constraints(col_schema.get("record_schemas", {}))
        errors = _validate_record({"status": "finished"}, "requirement", constraints, "test_req")
        assert len(errors) == 1

    def test_not_started_accepted(self):
        schema = _load_schema(BRAIN_DIR)
        col_schema = _find_collection_schema(schema, "agile_backlog")
        constraints = _extract_constraints(col_schema.get("record_schemas", {}))
        errors = _validate_record({"status": "not_started"}, "requirement", constraints, "test_req")
        assert errors == []


class TestIllegalApprovalRejected:
    """Approval field must be constrained by CV-011."""

    def test_yes_rejected(self):
        schema = _load_schema(BRAIN_DIR)
        col_schema = _find_collection_schema(schema, "agile_backlog")
        constraints = _extract_constraints(col_schema.get("record_schemas", {}))
        errors = _validate_record({"approval": "yes"}, "epic", constraints, "test_epic")
        assert len(errors) == 1

    def test_approved_accepted(self):
        schema = _load_schema(BRAIN_DIR)
        col_schema = _find_collection_schema(schema, "agile_backlog")
        constraints = _extract_constraints(col_schema.get("record_schemas", {}))
        errors = _validate_record({"approval": "approved"}, "epic", constraints, "test_epic")
        assert errors == []


class TestIllegalEpicTypeRejected:
    """Epic type must be operational or functional."""

    def test_technical_rejected(self):
        schema = _load_schema(BRAIN_DIR)
        col_schema = _find_collection_schema(schema, "agile_backlog")
        constraints = _extract_constraints(col_schema.get("record_schemas", {}))
        errors = _validate_record({"type": "technical"}, "epic", constraints, "test_epic")
        assert len(errors) == 1

    def test_operational_accepted(self):
        schema = _load_schema(BRAIN_DIR)
        col_schema = _find_collection_schema(schema, "agile_backlog")
        constraints = _extract_constraints(col_schema.get("record_schemas", {}))
        errors = _validate_record({"type": "operational"}, "epic", constraints, "test_epic")
        assert errors == []


class TestFullBacklogWithIllegalValue:
    """Inject an illegal value into the real backlog data and verify it's caught."""

    def test_injected_illegal_feature_status_caught(self):
        backlog_path = os.path.join(BRAIN_DIR, "machine_artifacts", "content", "agile_backlog.json")
        with open(backlog_path, encoding="utf-8") as f:
            data = json.load(f)

        # Deep copy and inject illegal value
        bad_data = copy.deepcopy(data)
        bad_data["epics"][0]["features"][0]["status"] = "ILLEGAL_VALUE"

        # Write to temp, validate, clean up
        import tempfile
        tmp_dir = tempfile.mkdtemp()
        tmp_brain = os.path.join(tmp_dir, "machine_artifacts", "content")
        os.makedirs(tmp_brain)

        # Copy schema
        import shutil
        shutil.copy(
            os.path.join(BRAIN_DIR, "machine_artifacts", "content", "schema.json"),
            os.path.join(tmp_brain, "schema.json"),
        )

        # Write bad backlog
        with open(os.path.join(tmp_brain, "agile_backlog.json"), "w", encoding="utf-8") as f:
            json.dump(bad_data, f)

        errors = validate_backlog(tmp_dir)
        shutil.rmtree(tmp_dir)

        assert len(errors) >= 1
        assert "ILLEGAL_VALUE" in errors[0]
