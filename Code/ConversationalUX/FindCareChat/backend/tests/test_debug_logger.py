# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
import os, sys, unittest
from unittest.mock import MagicMock, patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from infrastructure.debug_logger import DebugLogger


class TestDebugLogger(unittest.TestCase):

    def test_log_chat_with_no_db(self):
        logger = DebugLogger(get_db_fn=lambda: None, env_prefix="dev")
        # Should not raise
        logger.log_chat("1.2.3.4", "hello", 0, 0, 100, 50, "response", None)

    def test_log_chat_writes_to_db(self):
        mock_db = MagicMock()
        logger = DebugLogger(get_db_fn=lambda: mock_db, env_prefix="dev")
        logger.log_chat("1.2.3.4", "hello", 3, 1, 100, 50, "response", None)
        mock_db["dev_Debug"]["chat_calls"].insert_one.assert_called_once()
        record = mock_db["dev_Debug"]["chat_calls"].insert_one.call_args[0][0]
        self.assertEqual(record["ip"], "1.2.3.4")
        self.assertEqual(record["tokens_in"], 100)

    def test_log_chat_error_with_history_deidentifies(self):
        mock_db = MagicMock()
        mock_consent = MagicMock()
        logger = DebugLogger(get_db_fn=lambda: mock_db, env_prefix="dev", consent_service=mock_consent)
        history = [{"role": "user", "content": "my name is Skip"}]
        logger.log_chat("1.2.3.4", "test", 1, 0, None, None, None, "error occurred", history)
        mock_consent.de_identify.assert_called_once()

    def test_log_chat_error_without_consent_service(self):
        mock_db = MagicMock()
        logger = DebugLogger(get_db_fn=lambda: mock_db, env_prefix="dev")
        history = [{"role": "user", "content": "test"}]
        # Should not raise even without consent service
        logger.log_chat("1.2.3.4", "test", 1, 0, None, None, None, "error", history)

    def test_log_chat_db_failure_does_not_raise(self):
        mock_db = MagicMock()
        mock_db["dev_Debug"]["chat_calls"].insert_one.side_effect = Exception("DB down")
        logger = DebugLogger(get_db_fn=lambda: mock_db, env_prefix="dev")
        # Should not raise
        logger.log_chat("1.2.3.4", "test", 0, 0, None, None, None, None)


if __name__ == "__main__":
    unittest.main()
