# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# EPIC-8 Safety: EPIC-004-F-001-S-001 tests
# REQ-001: Admin unlock with shared secret
# REQ-002: Unlock code is case-insensitive
#
# AUDIT FIX 2026-04-15:
#   - 5 tests REMOVED that extracted the key from the service and did
#     assertIn(key.lower(), message.lower()) — testing Python string ops,
#     not the actual try_admin_unlock method.
#   - Replaced with tests that call the REAL try_admin_unlock method.
#   - test_code_check_in_service KEPT (source inspection, already valid).
#   - try_admin_unlock requires a DB to return True (it updates MongoDB),
#     so we use a mock DB to verify the method reaches the update call.

import os
import sys
import inspect
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestAdminUnlock(unittest.TestCase):
    """EPIC-004-F-001-S-001: Admin unlock with shared secret."""

    @classmethod
    def setUpClass(cls):
        from domain.shared.safety.safety_service import SafetyService
        cls.SafetyService = SafetyService

    def _make_service(self, key="unlock$123", db_fn=None):
        """Create a SafetyService with a mock DB that supports unlock."""
        if db_fn is None:
            mock_db = MagicMock()
            mock_collection = MagicMock()
            mock_collection.update_one.return_value = MagicMock(modified_count=1)
            mock_db.__getitem__ = MagicMock(return_value={"emergency_incidents": mock_collection})
            # Make dict-style access work: db[env_Safety][emergency_incidents]
            safety_db = MagicMock()
            safety_db.__getitem__ = MagicMock(return_value=mock_collection)
            mock_db.__getitem__ = MagicMock(return_value=safety_db)
            db_fn = lambda: mock_db
        return self.SafetyService(
            get_db_fn=db_fn,
            env_prefix="dev",
            emergency_keywords=[],
            admin_unlock_key=key,
        )

    # ── REQ-001: Unlock with correct code ──

    def test_correct_code_calls_try_admin_unlock(self):
        """try_admin_unlock with correct key in message returns True."""
        service = self._make_service(key="unlock$123")
        result = service.try_admin_unlock("please unlock$123 now", "127.0.0.1")
        self.assertTrue(result)

    def test_wrong_code_returns_false(self):
        """try_admin_unlock with wrong key returns False."""
        service = self._make_service(key="unlock$123")
        result = service.try_admin_unlock("please wrongcode now", "127.0.0.1")
        self.assertFalse(result)

    def test_empty_key_returns_false(self):
        """try_admin_unlock with empty admin key always returns False."""
        service = self._make_service(key="")
        result = service.try_admin_unlock("anything", "127.0.0.1")
        self.assertFalse(result)

    # ── REQ-002: Case-insensitive ──

    def test_case_insensitive_uppercase(self):
        """'UNLOCK$123' matches key 'unlock$123' via try_admin_unlock."""
        service = self._make_service(key="unlock$123")
        result = service.try_admin_unlock("UNLOCK$123", "127.0.0.1")
        self.assertTrue(result)

    def test_case_insensitive_mixed(self):
        """'Unlock$123' matches key 'unlock$123' via try_admin_unlock."""
        service = self._make_service(key="unlock$123")
        result = service.try_admin_unlock("Unlock$123", "127.0.0.1")
        self.assertTrue(result)

    def test_case_insensitive_in_sentence(self):
        """Key embedded in a sentence, mixed case, via try_admin_unlock."""
        service = self._make_service(key="unlock$123")
        result = service.try_admin_unlock("Hey can you UNLOCK$123 this for me", "127.0.0.1")
        self.assertTrue(result)

    # ── Source inspection ──

    def test_code_check_in_service(self):
        """Verify the actual service code does case-insensitive comparison."""
        source = inspect.getsource(self.SafetyService.try_admin_unlock)
        self.assertIn(".lower()", source,
                      "try_admin_unlock must use .lower() for case-insensitive comparison")

    # ── No DB returns False ──

    def test_no_db_returns_false(self):
        """try_admin_unlock returns False when DB is unavailable."""
        service = self._make_service(key="unlock$123", db_fn=lambda: None)
        result = service.try_admin_unlock("unlock$123", "127.0.0.1")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
