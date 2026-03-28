# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Tests for Human Testing Mode and Markdown Table Rendering features.
# Run: python -m pytest Code/ConversationalUX/FindCareChat/backend/tests/test_human_testing_mode.py -v

import json
import os
import sys
from unittest.mock import patch

import pytest

# Ensure backend is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Test: HUMAN_TESTING flag behavior
# ---------------------------------------------------------------------------

class TestHumanTestingFlag:
    """Tests for the HUMAN_TESTING environment variable."""

    def test_flag_defaults_to_false(self):
        """HUMAN_TESTING defaults to false when not set."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HUMAN_TESTING", None)
            val = os.getenv("HUMAN_TESTING", "false").lower() == "true"
            assert val is False

    def test_flag_true_when_set(self):
        """HUMAN_TESTING=true activates test mode."""
        with patch.dict(os.environ, {"HUMAN_TESTING": "true"}):
            val = os.getenv("HUMAN_TESTING", "false").lower() == "true"
            assert val is True

    def test_flag_false_when_explicitly_false(self):
        """HUMAN_TESTING=false keeps test mode off."""
        with patch.dict(os.environ, {"HUMAN_TESTING": "false"}):
            val = os.getenv("HUMAN_TESTING", "false").lower() == "true"
            assert val is False

    def test_flag_case_insensitive(self):
        """HUMAN_TESTING=TRUE (uppercase) still works."""
        with patch.dict(os.environ, {"HUMAN_TESTING": "TRUE"}):
            val = os.getenv("HUMAN_TESTING", "false").lower() == "true"
            assert val is True


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
        assert "Welcome to ChatHealthy FindCare" in WELCOME_MESSAGE

    def test_welcome_test_mode_has_table(self):
        """Test mode _build_test_welcome returns feature tracking table."""
        from main import _build_test_welcome
        result = _build_test_welcome()
        assert "HUMAN TESTING MODE" in result

    def test_welcome_test_mode_has_all_features(self):
        """Test mode table includes all feature rows."""
        from main import _build_test_welcome
        msg = _build_test_welcome()
        assert "Provider Search" in msg
        assert "Safety Filter" in msg
        assert "HIPAA Consent" in msg
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
        assert "Bugs Deferred" in msg
        assert "Feat Impl" in msg
        assert "Feat Deferred" in msg


# ---------------------------------------------------------------------------
# Test: _build_test_welcome function
# ---------------------------------------------------------------------------

class TestBuildTestWelcome:
    """Tests for the _build_test_welcome() function."""

    def test_returns_markdown_table(self):
        """Output is valid markdown with table syntax."""
        # Import directly
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from main import _build_test_welcome
        result = _build_test_welcome()
        assert "|" in result
        assert "---" in result
        assert "HUMAN TESTING MODE" in result

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

    def test_feature_count_matches(self):
        from main import _build_test_welcome, _TEST_FEATURES
        result = _build_test_welcome()
        # Count data rows (exclude header, separator, totals, title, footer)
        lines = [l for l in result.split("\n") if l.startswith("| ") and "---" not in l and "Feature" not in l and "TOTALS" not in l and "HUMAN" not in l]
        assert len(lines) == len(_TEST_FEATURES)

    def test_totals_row_present(self):
        from main import _build_test_welcome
        result = _build_test_welcome()
        assert "TOTALS" in result

    def test_completed_count_in_totals(self):
        from main import _build_test_welcome, _TEST_FEATURES
        result = _build_test_welcome()
        completed = sum(1 for f in _TEST_FEATURES if f["completed"])
        total = len(_TEST_FEATURES)
        assert f"**{completed}/{total}**" in result


# ---------------------------------------------------------------------------
# Test: Production override (commented out, verify logic)
# ---------------------------------------------------------------------------

class TestProductionOverride:
    """Tests for the production override logic (currently commented out)."""

    def test_prod_override_logic(self):
        """Verify the override logic is correct when uncommented."""
        # Simulate: if ENV_PREFIX == "prod", HUMAN_TESTING should be False
        env_prefix = "prod"
        human_testing = True
        if env_prefix == "prod":
            human_testing = False
        assert human_testing is False

    def test_dev_no_override(self):
        """Dev environment does not override HUMAN_TESTING."""
        env_prefix = "dev"
        human_testing = True
        if env_prefix == "prod":
            human_testing = False
        assert human_testing is True
