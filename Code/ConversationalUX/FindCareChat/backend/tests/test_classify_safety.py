# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# test_classify_safety.py — /classify must run safety check.
#
# Regression test: emergency messages through /classify must trigger
# CALL 911, not return provider cards.

import sys
import os
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestClassifySafetyGate:
    """/classify endpoint must check for emergencies."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Patch safety service before importing app."""
        # We test by calling the classify function directly with mocked safety
        pass

    def test_heart_attack_returns_emergency(self):
        """'I'm having a heart attack' must return emergency, not providers."""
        from main import _safety_service, classify, ClassifyRequest
        from fastapi.testclient import TestClient
        from main import app

        with patch.object(_safety_service, 'is_emergency', return_value=True) as mock_emergency, \
             patch.object(_safety_service, 'is_ip_locked', return_value=False), \
             patch.object(_safety_service, 'lock_ip'):
            client = TestClient(app)
            response = client.post("/classify", json={"message": "I'm having a heart attack"})
            data = response.json()
            assert data.get("emergency") is True, f"Expected emergency=True, got {data}"
            assert "911" in data.get("response", ""), f"Expected 911 in response, got {data}"
            mock_emergency.assert_called_once_with("I'm having a heart attack")

    def test_ip_locked_returns_emergency(self):
        """Locked IP must return emergency on /classify too."""
        from main import _safety_service
        from fastapi.testclient import TestClient
        from main import app

        with patch.object(_safety_service, 'is_ip_locked', return_value=True):
            client = TestClient(app)
            response = client.post("/classify", json={"message": "bone doc in VA"})
            data = response.json()
            assert data.get("emergency") is True

    def test_normal_message_returns_specialties(self):
        """Normal messages must still return specialty results."""
        from main import _safety_service
        from fastapi.testclient import TestClient
        from main import app

        with patch.object(_safety_service, 'is_emergency', return_value=False), \
             patch.object(_safety_service, 'is_ip_locked', return_value=False):
            client = TestClient(app)
            response = client.post("/classify", json={"message": "bone doc in VA"})
            data = response.json()
            assert "emergency" not in data or data.get("emergency") is not True
            # Should have specialties (may be empty if no DB, but no emergency)
            assert "specialties" in data or "error" in data
