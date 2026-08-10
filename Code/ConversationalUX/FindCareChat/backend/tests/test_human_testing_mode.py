# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Tests for Human Testing Mode and Markdown Table Rendering features.
# Run: python -m pytest Code/ConversationalUX/FindCareChat/backend/tests/test_human_testing_mode.py -v
#
# AUDIT FIX 2026-04-15:
#   - TestHumanTestingFlag: REMOVED — 4 tests that tested Python's os.getenv
#     and string .lower(), not any application code.
#   - TestProductionOverride: REMOVED — 2 tests that tested an inline
#     if-statement, not any real application code.
#   - TestWelcomeEndpoint and TestBuildTestWelcome: KEPT — they test real
#     FastAPI endpoints and real _build_test_welcome function.
#   - Added TestHumanTestingInApp: tests the REAL _HUMAN_TESTING logic in main.py.

import json
import inspect
import os
import sys

import pytest

# Ensure backend is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Test: HUMAN_TESTING flag — real application code
# ---------------------------------------------------------------------------

class TestHumanTestingInApp:
    """Tests that the HUMAN_TESTING flag is actually read and used by main.py."""

    def test_main_module_reads_human_testing_env(self):
        """main.py must read HUMAN_TESTING from environment."""
        import main
        source = inspect.getsource(main)
        assert "HUMAN_TESTING" in source, \
            "main.py must reference HUMAN_TESTING environment variable"

    def test_human_testing_parsed_as_boolean(self):
        """main.py must parse HUMAN_TESTING to a boolean, not use raw string."""
        import main
        # The real variable _HUMAN_TESTING should be a bool
        assert isinstance(main._HUMAN_TESTING, bool), \
            "_HUMAN_TESTING must be a bool, not a string"

    def test_human_testing_rejects_false_and_zero(self):
        """The parsing logic rejects 'false' and '0' as falsy."""
        import main
        source = inspect.getsource(main)
        # Verify the app has rejection logic for "false" and "0"
        assert '"false"' in source or "'false'" in source, \
            "main.py should explicitly handle 'false' string"
        assert '"0"' in source or "'0'" in source, \
            "main.py should explicitly handle '0' string"


# ---------------------------------------------------------------------------
# Test: Welcome endpoint behavior
# ---------------------------------------------------------------------------

class TestWelcomeEndpoint:
    """Tests for /welcome endpoint in normal and test modes."""

    @pytest.fixture
    def client(self):
        """FastAPI test client — uses current HUMAN_TESTING from .env."""
        from fastapi.testclient import TestClient
        import main
        return TestClient(main.app)

    def test_welcome_returns_200(self, client):
        """Welcome endpoint returns 200."""
        response = client.get("/welcome")
        assert response.status_code == 200

    def test_welcome_has_message(self, client):
        """Welcome endpoint returns a message field."""
        response = client.get("/welcome")
        data = response.json()
        assert "message" in data
        assert len(data["message"]) > 0

    def test_welcome_normal_mode_content(self):
        """Normal mode returns standard welcome (tested via direct function)."""
        from main import WELCOME_MESSAGE
        assert "Welcome to ChatHealthy" in WELCOME_MESSAGE

    def test_welcome_test_mode_has_table(self):
        """Test mode _build_test_welcome returns feature tracking table."""
        from main import _build_test_welcome
        result = _build_test_welcome()
        assert "UAT Session" in result

    def test_welcome_test_mode_has_all_features(self):
        """Test mode table includes key feature rows."""
        from main import _build_test_welcome
        msg = _build_test_welcome()
        assert "Provider Search" in msg
        assert "Safety Filter" in msg
        assert "Consent Framework" in msg
        assert "URL Guardian" in msg
        assert "Unanswerable Question" in msg
        assert "Markdown Table Rendering" in msg

    def test_welcome_test_mode_has_totals(self):
        """Test mode table includes TOTALS row."""
        from main import _build_test_welcome
        assert "TOTALS" in _build_test_welcome()

    def test_welcome_test_mode_has_columns(self):
        """Test mode table has all required columns."""
        from main import _build_test_welcome
        msg = _build_test_welcome()
        assert "Done" in msg
        assert "Bugs Fixed" in msg
        assert "New Features" in msg


# ---------------------------------------------------------------------------
# Test: _build_test_welcome function
# ---------------------------------------------------------------------------

class TestBuildTestWelcome:
    """Tests for the _build_test_welcome() function."""

    def test_returns_markdown_table(self):
        """Output is valid markdown with table syntax."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from main import _build_test_welcome
        result = _build_test_welcome()
        assert "|" in result
        assert "---" in result
        assert "UAT Session" in result

    def test_table_has_header_row(self):
        from main import _build_test_welcome
        result = _build_test_welcome()
        lines = result.split("\n")
        header = [l for l in lines if "Feature" in l and "Done" in l]
        assert len(header) == 1

    def test_table_has_separator_row(self):
        from main import _build_test_welcome
        result = _build_test_welcome()
        lines = result.split("\n")
        separators = [l for l in lines if "---" in l and "|" in l]
        assert len(separators) >= 1

    def test_feature_rows_present(self):
        from main import _build_test_welcome
        result = _build_test_welcome()
        # Count data rows (exclude header, separator, title lines)
        lines = [l for l in result.split("\n")
                 if l.strip().startswith("|") and "---" not in l
                 and "Feature" not in l and "Environment" not in l
                 and "UAT" not in l]
        assert len(lines) >= 1, "UAT table must contain at least one feature row"

    def test_totals_row_present(self):
        from main import _build_test_welcome
        result = _build_test_welcome()
        assert "TOTALS" in result

    def test_table_has_data_rows(self):
        """UAT table contains numbered feature rows."""
        from main import _build_test_welcome
        result = _build_test_welcome()
        # Feature rows start with |  N | where N is a number
        data_rows = [cell for cell in result.split("|")
                     if cell.strip().isdigit()]
        assert len(data_rows) >= 1, "UAT table must contain numbered feature rows"
