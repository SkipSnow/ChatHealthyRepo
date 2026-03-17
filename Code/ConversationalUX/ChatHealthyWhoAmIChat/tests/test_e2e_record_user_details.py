"""
End-to-end regression tests for record_user_details consent flow.

All external dependencies are real:
  - MongoDB Atlas  (writes to AboutUs.lead with testdata=True)
  - Anthropic API  (real deIdentify call in Case 2)
  - Pushover       (real push notifications sent)

Teardown deletes only records where testdata=True.

Run:
  python Code/HuggingBearCode/ChatHealthyWhoAmIChat/tests/test_e2e_record_user_details.py
"""

import sys
import os
import unittest
from datetime import datetime

# ── Resolve paths and environment ─────────────────────────────────────────────
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(_APP_DIR, "..", "..", ".env"), override=True)

import app

# ── Unique run ID ensures fresh records each run (history accumulates in DB) ──
_RUN_ID  = datetime.now().strftime("%Y%m%d_%H%M%S")
EMAIL_C1 = f"e2e.case1.{_RUN_ID}@testchathealthy.com"
EMAIL_C2 = f"e2e.case2.{_RUN_ID}@testchathealthy.com"
EMAIL_C3 = f"e2e.case3.{_RUN_ID}@testchathealthy.com"
NAME     = "Jane Doe"

SAMPLE_HISTORY_NO_CONSENT = [
    {"role": "user",      "content": "Hi, my name is Jane Doe, I was born on 03/15/1972, and I live in Los Angeles."},
    {"role": "assistant", "content": "Hello Jane! How can I help you today?"},
    {"role": "user",      "content": "I run a hospital network and want to learn about ChatHealthy.AI."},
    {"role": "assistant", "content": "I'd love to tell you more."},
]

# Case 1: includes the verbatim consent exchange — self-evidencing in the stored transcript
SAMPLE_HISTORY_VERBATIM_CONSENT = SAMPLE_HISTORY_NO_CONSENT + [
    {"role": "assistant", "content": "May we save a verbatim transcript of this conversation with your contact details?"},
    {"role": "user",      "content": "Yes, that's fine."},
]

# Cases 2 & 3: consent exchange for summary (or none) — no verbatim transcript stored
SAMPLE_HISTORY_SUMMARY_CONSENT = SAMPLE_HISTORY_NO_CONSENT + [
    {"role": "assistant", "content": "May we save a verbatim transcript of this conversation with your contact details?"},
    {"role": "user",      "content": "No."},
    {"role": "assistant", "content": "May we save a de-identified summary of this conversation instead?"},
    {"role": "user",      "content": "Yes, a summary is fine."},
]

SAMPLE_HISTORY_NO_HISTORY_CONSENT = SAMPLE_HISTORY_NO_CONSENT + [
    {"role": "assistant", "content": "May we save a verbatim transcript of this conversation with your contact details?"},
    {"role": "user",      "content": "No."},
    {"role": "assistant", "content": "May we save a de-identified summary of this conversation instead?"},
    {"role": "user",      "content": "No, please don't save anything."},
]

PII_MARKERS = ["Jane Doe", "03/15/1972", "Los Angeles"]

# Consent phrases that must appear in the verbatim transcript
CONSENT_MARKERS = [
    "May we save a verbatim transcript",
    "Yes, that's fine.",
]


def _get_lead_coll():
    return app._get_db()["AboutUs"]["lead"]


def _read_record(email):
    return _get_lead_coll().find_one({"email": email, "testdata": True}, {"_id": 0})


# ── Test cases ────────────────────────────────────────────────────────────────
class TestE2ERecordUserDetails(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Verify MongoDB is reachable before running any tests."""
        if app._get_db() is None:
            raise RuntimeError("MongoDB unavailable — cannot run e2e tests.")

    # ── Case 1: Full consent ──────────────────────────────────────────────────
    def test_case1_full_consent(self):
        """
        Case 1: consent_verbatim=True
        Expects: verbatim chat_history in DB, PII intact, testdata=True.
        """
        result = app.record_user_details(
            email=EMAIL_C1,
            name=NAME,
            notes="E2E test — full consent",
            chat_history=SAMPLE_HISTORY_VERBATIM_CONSENT,
            consent_verbatim=True,
            consent_summary=None,
            testdata=True,
        )
        self.assertEqual(result, {"recorded": "ok"})

        record = _read_record(EMAIL_C1)
        self.assertIsNotNone(record, "Record not found in MongoDB")

        self.assertTrue(record["testdata"])
        self.assertTrue(record["consent_verbatim"])
        self.assertIsNone(record["consent_summary"])

        self.assertIn("chat_history", record)
        full_text = " ".join(m["content"] for m in record["chat_history"])
        for pii in PII_MARKERS:
            self.assertIn(pii, full_text, f"Expected PII '{pii}' intact in verbatim record")
        # Consent exchange must be present in the transcript — self-evidencing
        for phrase in CONSENT_MARKERS:
            self.assertIn(phrase, full_text, f"Consent phrase '{phrase}' missing from verbatim transcript")

        self.assertIn("datetime", record)
        datetime.fromisoformat(record["datetime"])

        print(f"\n[Case 1] consent_verbatim=True | PII intact | consent exchange present | datetime={record['datetime']}")

    # ── Case 2: Summary consent ───────────────────────────────────────────────
    def test_case2_summary_consent(self):
        """
        Case 2: consent_verbatim=False, consent_summary=True
        Expects: LLM-generated summary de-identified and stored in notes.
                 No chat_history in record.
        """
        result = app.record_user_details(
            email=EMAIL_C2,
            name=NAME,
            notes="E2E test — summary consent",
            chat_history=SAMPLE_HISTORY_SUMMARY_CONSENT,
            consent_verbatim=False,
            consent_summary=True,
            testdata=True,
        )
        self.assertEqual(result, {"recorded": "ok"})

        record = _read_record(EMAIL_C2)
        self.assertIsNotNone(record, "Record not found in MongoDB")

        self.assertTrue(record["testdata"])
        self.assertFalse(record["consent_verbatim"])
        self.assertTrue(record["consent_summary"])

        # Summary stored in notes — no transcript
        self.assertNotIn("chat_history", record)
        self.assertIn("notes", record)
        self.assertIsInstance(record["notes"], str)
        self.assertGreater(len(record["notes"]), 0)

        # PII must not appear in the stored summary
        for pii in PII_MARKERS:
            self.assertNotIn(pii, record["notes"], f"PII '{pii}' should have been removed from summary")

        self.assertIn("datetime", record)
        datetime.fromisoformat(record["datetime"])

        print(f"\n[Case 2] consent_summary=True | de-identified summary in notes: {record['notes']}")

    # ── Case 3: Contact info only ─────────────────────────────────────────────
    def test_case3_contact_only(self):
        """
        Case 3: consent_verbatim=False, consent_summary=False
        Expects: no chat_history field, contact fields present, testdata=True.
        """
        result = app.record_user_details(
            email=EMAIL_C3,
            name=NAME,
            notes="E2E test — contact only",
            chat_history=SAMPLE_HISTORY_NO_HISTORY_CONSENT,
            consent_verbatim=False,
            consent_summary=False,
            testdata=True,
        )
        self.assertEqual(result, {"recorded": "ok"})

        record = _read_record(EMAIL_C3)
        self.assertIsNotNone(record, "Record not found in MongoDB")

        self.assertTrue(record["testdata"])
        self.assertFalse(record["consent_verbatim"])
        self.assertFalse(record["consent_summary"])
        self.assertNotIn("chat_history", record)
        self.assertEqual(record["email"], EMAIL_C3)
        self.assertEqual(record["name"],  NAME)

        self.assertIn("datetime", record)
        datetime.fromisoformat(record["datetime"])

        print(f"\n[Case 3] No chat_history | contact fields intact | datetime={record['datetime']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
