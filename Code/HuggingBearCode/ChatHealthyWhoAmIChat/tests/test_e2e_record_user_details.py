"""
End-to-end regression tests for record_user_details consent flow.

All external dependencies are real:
  - MongoDB Atlas  (writes to AboutUs.lead_e2e_test, never to lead)
  - Anthropic API  (real deIdentify call in Case 2)
  - Pushover       (real push notifications sent)

Teardown deletes only records written by this test run.

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

# ── Test collection — never touches production 'lead' ─────────────────────────
TEST_COLLECTION = "lead_e2e_test"
TEST_DB         = "AboutUs"

# ── Shared fixtures ───────────────────────────────────────────────────────────
EMAIL_C1 = "e2e.case1@testchatheatlhy.com"
EMAIL_C2 = "e2e.case2@testchathealthy.com"
EMAIL_C3 = "e2e.case3@testchathealthy.com"
NAME     = "Jane Doe"

SAMPLE_HISTORY = [
    {"role": "user",      "content": "Hi, my name is Jane Doe, I was born on 03/15/1972, and I live in Los Angeles."},
    {"role": "assistant", "content": "Hello Jane! How can I help you today?"},
    {"role": "user",      "content": "I run a hospital network and want to learn about ChatHealthy.AI."},
    {"role": "assistant", "content": "I'd love to tell you more about ChatHealthy.AI."},
]

PII_MARKERS = ["Jane Doe", "03/15/1972", "Los Angeles"]


def _get_test_coll():
    """Return the live e2e test collection."""
    db = app._get_db()
    return db[TEST_DB][TEST_COLLECTION]


def _read_record(email):
    """Read a single record from the test collection by email."""
    coll = _get_test_coll()
    return coll.find_one({"email": email}, {"_id": 0})


def _delete_record(email):
    """Remove a test record by email."""
    _get_test_coll().delete_many({"email": email})


def _write_record(email, name, notes, chat_history, consent_verbatim, consent_summary):
    """
    Calls record_user_details but routes the insert to the e2e test collection
    by temporarily monkey-patching commitSignificantActivity to redirect the
    collection name. All other logic (deIdentify, push, datetime, etc.) runs
    as-is.
    """
    original_commit = app.commitSignificantActivity

    def _redirected_commit(payload=None, **kwargs):
        payload = payload or kwargs
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)
        # Redirect to test collection
        payload["collection"] = TEST_COLLECTION
        return original_commit(payload)

    app.commitSignificantActivity = _redirected_commit
    try:
        result = app.record_user_details(
            email=email,
            name=name,
            notes=notes,
            chat_history=chat_history,
            consent_verbatim=consent_verbatim,
            consent_summary=consent_summary,
        )
    finally:
        app.commitSignificantActivity = original_commit

    return result


# ── Test cases ────────────────────────────────────────────────────────────────
class TestE2ERecordUserDetails(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Verify MongoDB is reachable before running any tests."""
        db = app._get_db()
        if db is None:
            raise RuntimeError("MongoDB unavailable — cannot run e2e tests.")
        # Clean up any leftover records from a previous failed run
        for email in (EMAIL_C1, EMAIL_C2, EMAIL_C3):
            _delete_record(email)

    @classmethod
    def tearDownClass(cls):
        """Delete all records written by this test run."""
        for email in (EMAIL_C1, EMAIL_C2, EMAIL_C3):
            _delete_record(email)
        print("\nTeardown complete — all e2e test records deleted.")

    # ── Case 1: Full consent ──────────────────────────────────────────────────
    def test_case1_full_consent(self):
        """
        Case 1: consent_verbatim=True
        Expects: verbatim chat_history in DB, PII intact, correct consent fields.
        """
        result = _write_record(
            email=EMAIL_C1,
            name=NAME,
            notes="E2E test — full consent",
            chat_history=SAMPLE_HISTORY,
            consent_verbatim=True,
            consent_summary=None,
        )
        self.assertEqual(result, {"recorded": "ok"})

        record = _read_record(EMAIL_C1)
        self.assertIsNotNone(record, "Record not found in MongoDB")

        # Consent flags
        self.assertTrue(record["consent_verbatim"])
        self.assertIsNone(record["consent_summary"])

        # Verbatim: PII must be intact
        self.assertIn("chat_history", record)
        full_text = " ".join(m["content"] for m in record["chat_history"])
        for pii in PII_MARKERS:
            self.assertIn(pii, full_text, f"Expected PII '{pii}' to be present in verbatim record")

        # Datetime
        self.assertIn("datetime", record)
        datetime.fromisoformat(record["datetime"])  # must be valid ISO format

        # Contact fields
        self.assertEqual(record["email"], EMAIL_C1)
        self.assertEqual(record["name"],  NAME)

        print(f"\n[Case 1] Record stored — consent_verbatim=True, PII intact, datetime={record['datetime']}")

    # ── Case 2: Summary consent ───────────────────────────────────────────────
    def test_case2_summary_consent(self):
        """
        Case 2: consent_verbatim=False, consent_summary=True
        Expects: de-identified chat_history in DB (real Anthropic call),
                 PII removed, original SAMPLE_HISTORY untouched.
        """
        original_first = SAMPLE_HISTORY[0]["content"]

        result = _write_record(
            email=EMAIL_C2,
            name=NAME,
            notes="E2E test — summary consent",
            chat_history=SAMPLE_HISTORY,
            consent_verbatim=False,
            consent_summary=True,
        )
        self.assertEqual(result, {"recorded": "ok"})

        record = _read_record(EMAIL_C2)
        self.assertIsNotNone(record, "Record not found in MongoDB")

        # Consent flags
        self.assertFalse(record["consent_verbatim"])
        self.assertTrue(record["consent_summary"])

        # De-identified: PII must NOT appear in stored messages
        self.assertIn("chat_history", record)
        full_text = " ".join(m["content"] for m in record["chat_history"])
        for pii in PII_MARKERS:
            self.assertNotIn(pii, full_text, f"PII '{pii}' should have been removed by deIdentify")

        # Deep copy verified: original SAMPLE_HISTORY must be untouched
        self.assertEqual(SAMPLE_HISTORY[0]["content"], original_first,
                         "deIdentify mutated original SAMPLE_HISTORY — deep copy failed")

        # Datetime
        self.assertIn("datetime", record)
        datetime.fromisoformat(record["datetime"])

        print(f"\n[Case 2] Record stored — consent_summary=True, PII removed, datetime={record['datetime']}")
        print(f"         Stored history sample: {record['chat_history'][0]['content'][:80]}")

    # ── Case 3: Contact info only ─────────────────────────────────────────────
    def test_case3_contact_only(self):
        """
        Case 3: consent_verbatim=False, consent_summary=False
        Expects: no chat_history field in DB at all, contact fields present.
        """
        result = _write_record(
            email=EMAIL_C3,
            name=NAME,
            notes="E2E test — contact only",
            chat_history=SAMPLE_HISTORY,
            consent_verbatim=False,
            consent_summary=False,
        )
        self.assertEqual(result, {"recorded": "ok"})

        record = _read_record(EMAIL_C3)
        self.assertIsNotNone(record, "Record not found in MongoDB")

        # Consent flags
        self.assertFalse(record["consent_verbatim"])
        self.assertFalse(record["consent_summary"])

        # chat_history must be entirely absent
        self.assertNotIn("chat_history", record,
                         "chat_history should not be stored when both consents declined")

        # Contact fields must be present
        self.assertEqual(record["email"], EMAIL_C3)
        self.assertEqual(record["name"],  NAME)

        # Datetime
        self.assertIn("datetime", record)
        datetime.fromisoformat(record["datetime"])

        print(f"\n[Case 3] Record stored — no chat_history, datetime={record['datetime']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
