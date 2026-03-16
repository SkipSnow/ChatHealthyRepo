"""
Regression tests for record_user_details consent flow.

Three cases:
  Case 1 — Full consent:      verbatim transcript saved, no de-identification
  Case 2 — Summary consent:   de-identified summary saved, original history untouched
  Case 3 — Contact info only: no chat_history in record at all

Run:
  python -m pytest Code/HuggingBearCode/ChatHealthyWhoAmIChat/tests/ -v
  python Code/HuggingBearCode/ChatHealthyWhoAmIChat/tests/test_record_user_details.py
"""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

# ── Stub all heavy external dependencies before importing app ─────────────────
_STUBS = [
    "openai", "anthropic", "gradio", "pypdf", "requests",
    "dotenv", "ChatHealthyMongoUtilities",
]
for _mod in _STUBS:
    sys.modules.setdefault(_mod, MagicMock())

# Provide minimal env vars so module-level app code doesn't crash
os.environ.setdefault("MONGO_connectionString", "")
os.environ.setdefault("OPENAI_API_KEY",         "test-key")
os.environ.setdefault("Anthropic_API_KEY",       "test-key")
os.environ.setdefault("PUSHOVER_USER",           "test")
os.environ.setdefault("PUSHOVER_TOKEN",          "test")

# Add app directory to path and import
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)
import app  # noqa: E402

# ── Shared test fixtures ──────────────────────────────────────────────────────
EMAIL = "john.smith@hospital.org"
NAME  = "John Smith"
NOTES = "Interested in hospital network management."

# Contains PII — used to verify verbatim vs de-identified behaviour
SAMPLE_HISTORY = [
    {"role": "user",      "content": "Hi, I am John Smith, DOB 01/01/1980, from New York."},
    {"role": "assistant", "content": "Hello John! How can I help you today?"},
    {"role": "user",      "content": "I want to learn more about ChatHealthy for my hospital."},
    {"role": "assistant", "content": "I'd be happy to tell you more."},
]


def _make_mock_db():
    """Return a MagicMock DB client that simulates an empty lead collection."""
    mock_coll = MagicMock()
    mock_coll.find.return_value = []
    mock_db = MagicMock()
    # db["AboutUs"]["lead"] -> mock_coll
    mock_db.__getitem__.return_value.__getitem__.return_value = mock_coll
    return mock_db


def _fake_deidentify(history):
    """Simulates deIdentify: replaces all message content in-place."""
    for msg in history:
        msg["content"] = "[DEIDENTIFIED]"


# ── Test cases ────────────────────────────────────────────────────────────────
class TestRecordUserDetailsConsentFlow(unittest.TestCase):

    # ── Case 1 ────────────────────────────────────────────────────────────────
    @patch("app.push")
    @patch("app.commitSignificantActivity")
    @patch("app._get_db")
    def test_case1_full_consent_saves_verbatim(self, mock_get_db, mock_commit, mock_push):
        """Case 1: consent_verbatim=True — verbatim chat_history saved, deIdentify NOT called."""
        mock_get_db.return_value = _make_mock_db()

        with patch("app.deIdentify") as mock_deidentify:
            result = app.record_user_details(
                email=EMAIL,
                name=NAME,
                notes=NOTES,
                chat_history=SAMPLE_HISTORY,
                consent_verbatim=True,
                consent_summary=None,
            )

        # deIdentify must NOT be called for verbatim consent
        mock_deidentify.assert_not_called()

        mock_commit.assert_called_once()
        record = mock_commit.call_args[0][0]["record"]

        self.assertEqual(record["consent_verbatim"], True)
        self.assertIsNone(record["consent_summary"])
        self.assertEqual(record["chat_history"], SAMPLE_HISTORY)
        # PII must still be present (verbatim — not scrubbed)
        self.assertIn("John Smith", record["chat_history"][0]["content"])
        # Explicit datetime must be present
        self.assertIn("datetime", record)
        self.assertIsInstance(record["datetime"], str)
        self.assertEqual(result, {"recorded": "ok"})

    # ── Case 2 ────────────────────────────────────────────────────────────────
    @patch("app.push")
    @patch("app.commitSignificantActivity")
    @patch("app._get_db")
    def test_case2_summary_consent_deidentifies(self, mock_get_db, mock_commit, mock_push):
        """Case 2: consent_verbatim=False, consent_summary=True — de-identified copy saved.
        Original SAMPLE_HISTORY must be untouched (deep copy verified)."""
        mock_get_db.return_value = _make_mock_db()
        original_first_content = SAMPLE_HISTORY[0]["content"]

        with patch("app.deIdentify", side_effect=_fake_deidentify) as mock_deidentify:
            result = app.record_user_details(
                email=EMAIL,
                name=NAME,
                notes=NOTES,
                chat_history=SAMPLE_HISTORY,
                consent_verbatim=False,
                consent_summary=True,
            )

        # deIdentify MUST be called exactly once
        mock_deidentify.assert_called_once()

        mock_commit.assert_called_once()
        record = mock_commit.call_args[0][0]["record"]

        self.assertEqual(record["consent_verbatim"], False)
        self.assertEqual(record["consent_summary"], True)
        # Saved content must be de-identified
        for msg in record["chat_history"]:
            self.assertEqual(msg["content"], "[DEIDENTIFIED]")
        # Original SAMPLE_HISTORY must be untouched (deep copy, not shallow)
        self.assertEqual(SAMPLE_HISTORY[0]["content"], original_first_content)
        # Explicit datetime must be present
        self.assertIn("datetime", record)
        self.assertIsInstance(record["datetime"], str)
        self.assertEqual(result, {"recorded": "ok"})

    # ── Case 3 ────────────────────────────────────────────────────────────────
    @patch("app.push")
    @patch("app.commitSignificantActivity")
    @patch("app._get_db")
    def test_case3_no_consent_omits_chat_history(self, mock_get_db, mock_commit, mock_push):
        """Case 3: consent_verbatim=False, consent_summary=False — no chat_history saved."""
        mock_get_db.return_value = _make_mock_db()

        with patch("app.deIdentify") as mock_deidentify:
            result = app.record_user_details(
                email=EMAIL,
                name=NAME,
                notes=NOTES,
                chat_history=SAMPLE_HISTORY,
                consent_verbatim=False,
                consent_summary=False,
            )

        # deIdentify must NOT be called when both consents are declined
        mock_deidentify.assert_not_called()

        mock_commit.assert_called_once()
        record = mock_commit.call_args[0][0]["record"]

        self.assertEqual(record["consent_verbatim"], False)
        self.assertEqual(record["consent_summary"], False)
        # chat_history must be entirely absent from the record
        self.assertNotIn("chat_history", record)
        # Contact fields must still be present
        self.assertEqual(record["email"], EMAIL)
        self.assertEqual(record["name"], NAME)
        # Explicit datetime must be present
        self.assertIn("datetime", record)
        self.assertIsInstance(record["datetime"], str)
        self.assertEqual(result, {"recorded": "ok"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
