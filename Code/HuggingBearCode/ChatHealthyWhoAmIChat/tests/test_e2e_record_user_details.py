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

# ── Shared fixtures ───────────────────────────────────────────────────────────
EMAIL_C1 = "e2e.case1@testchathealthy.com"
EMAIL_C2 = "e2e.case2@testchathealthy.com"
EMAIL_C3 = "e2e.case3@testchathealthy.com"
NAME     = "Jane Doe"

SAMPLE_HISTORY = [
    {"role": "user",      "content": "Hi, my name is Jane Doe, I was born on 03/15/1972, and I live in Los Angeles."},
    {"role": "assistant", "content": "Hello Jane! How can I help you today?"},
    {"role": "user",      "content": "I run a hospital network and want to learn about ChatHealthy.AI."},
    {"role": "assistant", "content": "I'd love to tell you more."},
]

PII_MARKERS = ["Jane Doe", "03/15/1972", "Los Angeles"]


def _get_lead_coll():
    return app._get_db()["AboutUs"]["lead"]


def _read_record(email):
    return _get_lead_coll().find_one({"email": email, "testdata": True}, {"_id": 0})


# ── Test cases ────────────────────────────────────────────────────────────────
class TestE2ERecordUserDetails(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Verify MongoDB is reachable and clean up any prior test records."""
        if app._get_db() is None:
            raise RuntimeError("MongoDB unavailable — cannot run e2e tests.")
        _get_lead_coll().delete_many({"testdata": True})

    @classmethod
    def tearDownClass(cls):
        """Delete all automated test records from lead collection."""
        deleted = _get_lead_coll().delete_many({"testdata": True})
        print(f"\nTeardown complete — {deleted.deleted_count} test record(s) deleted from lead.")

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
            chat_history=SAMPLE_HISTORY,
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

        self.assertIn("datetime", record)
        datetime.fromisoformat(record["datetime"])

        print(f"\n[Case 1] consent_verbatim=True | PII intact | datetime={record['datetime']}")

    # ── Case 2: Summary consent ───────────────────────────────────────────────
    def test_case2_summary_consent(self):
        """
        Case 2: consent_verbatim=False, consent_summary=True
        Expects: de-identified chat_history (real Anthropic call), PII removed,
                 original SAMPLE_HISTORY untouched (deep copy verified).
        """
        original_first = SAMPLE_HISTORY[0]["content"]

        result = app.record_user_details(
            email=EMAIL_C2,
            name=NAME,
            notes="E2E test — summary consent",
            chat_history=SAMPLE_HISTORY,
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

        self.assertIn("chat_history", record)
        full_text = " ".join(m["content"] for m in record["chat_history"])
        for pii in PII_MARKERS:
            self.assertNotIn(pii, full_text, f"PII '{pii}' should have been removed by deIdentify")

        self.assertEqual(SAMPLE_HISTORY[0]["content"], original_first,
                         "deIdentify mutated original SAMPLE_HISTORY — deep copy failed")

        self.assertIn("datetime", record)
        datetime.fromisoformat(record["datetime"])

        print(f"\n[Case 2] consent_summary=True | PII removed | sample: {record['chat_history'][0]['content'][:80]}")

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
            chat_history=SAMPLE_HISTORY,
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
