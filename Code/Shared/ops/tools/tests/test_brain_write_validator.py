# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Tests for brain_write_validator.py, aligned to ChatHealthyBugsSchema v1
# (top-level "bug" array, records keyed by "bugid"/"title"/"description"/
# "reproduction").

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def validator():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("brain_write_validator")
    return mod


class TestFindDuplicateBug:
    def test_exact_duplicate_flagged(self, validator):
        existing = [{"bugid": "BUG-001", "title": "Login button is broken on mobile",
                     "description": "", "reproduction": ""}]
        result = validator.find_duplicate_bug(
            "Login button is broken on mobile", existing, threshold=0.6
        )
        assert result is not None
        assert result["match"]["bugid"] == "BUG-001"
        assert result["score"] >= 0.9

    def test_near_duplicate_flagged(self, validator):
        existing = [{"bugid": "BUG-002",
                     "title": "The login button is broken on mobile devices",
                     "description": "", "reproduction": ""}]
        result = validator.find_duplicate_bug(
            "Login button broken on mobile", existing, threshold=0.55
        )
        assert result is not None
        assert result["match"]["bugid"] == "BUG-002"

    def test_dissimilar_not_flagged(self, validator):
        existing = [{"bugid": "BUG-003", "title": "Login fails with 500 error",
                     "description": "", "reproduction": ""}]
        result = validator.find_duplicate_bug(
            "The database is running slow on weekends", existing, threshold=0.6
        )
        assert result is None

    def test_empty_list_returns_none(self, validator):
        assert validator.find_duplicate_bug("any text", []) is None

    def test_empty_proposed_returns_none(self, validator):
        existing = [{"bugid": "BUG-004", "title": "something",
                     "description": "", "reproduction": ""}]
        assert validator.find_duplicate_bug("", existing) is None

    def test_returns_highest_score_match(self, validator):
        existing = [
            {"bugid": "BUG-A", "title": "Login fails with 500 error",
             "description": "", "reproduction": ""},
            {"bugid": "BUG-B", "title": "Login fails with 500 error on mobile",
             "description": "", "reproduction": ""},
            {"bugid": "BUG-C", "title": "Database slow query",
             "description": "", "reproduction": ""},
        ]
        result = validator.find_duplicate_bug(
            "Login fails with 500 error", existing, threshold=0.6
        )
        assert result is not None
        assert result["match"]["bugid"] in ("BUG-A", "BUG-B")


class TestValidateBugInsert:
    def test_invalid_mode_raises(self, validator):
        with pytest.raises(ValueError):
            validator.validate_bug_insert({"title": "x"}, mode="bogus", bugs_list=[])

    def test_novel_bug_with_req_id_insert(self, validator):
        verdict = validator.validate_bug_insert(
            {"title": "Something brand-new with specific content xyz",
             "description": "", "reproduction": "", "req_id": "SOME-REQ-001"},
            mode="normal", bugs_list=[],
        )
        assert verdict["ok"] is True
        assert verdict["action"] == "INSERT"

    def test_duplicate_blocked_with_update_action(self, validator):
        existing = [{"bugid": "BUG-EX-001", "title": "Login fails with 500 error",
                     "description": "", "reproduction": ""}]
        verdict = validator.validate_bug_insert(
            {"title": "Login fails with 500 error",
             "description": "", "reproduction": "", "req_id": "SOME-REQ-001"},
            mode="normal", bugs_list=existing,
        )
        assert verdict["ok"] is False
        assert verdict["action"].startswith("UPDATE")
        assert verdict["match_id"] == "BUG-EX-001"

    def test_no_req_id_unattended_inserts_as_orphan(self, validator):
        verdict = validator.validate_bug_insert(
            {"title": "Novel bug content XYZ abc",
             "description": "", "reproduction": ""},
            mode="unattended", bugs_list=[],
        )
        assert verdict["ok"] is True
        assert verdict["action"] == "INSERT_AS_ORPHAN"

    def test_no_req_id_normal_escalates(self, validator):
        verdict = validator.validate_bug_insert(
            {"title": "Novel bug content XYZ abc",
             "description": "", "reproduction": ""},
            mode="normal", bugs_list=[],
        )
        assert verdict["ok"] is False
        assert verdict["action"] == "ESCALATE"

    def test_no_req_id_idiot_refuses(self, validator):
        verdict = validator.validate_bug_insert(
            {"title": "Novel bug content XYZ abc",
             "description": "", "reproduction": ""},
            mode="idiot", bugs_list=[],
        )
        assert verdict["ok"] is False
        assert verdict["action"] == "REFUSE"


class TestLoadBugs:
    def test_missing_file_returns_empty(self, validator, tmp_path, monkeypatch):
        monkeypatch.setattr(validator, "BRAIN_DIR", tmp_path)
        assert validator._load_bugs() == []

    def test_reads_bug_array(self, validator, tmp_path, monkeypatch):
        monkeypatch.setattr(validator, "BRAIN_DIR", tmp_path)
        (tmp_path / "bugs.json").write_text(
            '{"bug": [{"bugid": "BUG-1"}]}', encoding="utf-8"
        )
        bugs = validator._load_bugs()
        assert len(bugs) == 1
        assert bugs[0]["bugid"] == "BUG-1"
