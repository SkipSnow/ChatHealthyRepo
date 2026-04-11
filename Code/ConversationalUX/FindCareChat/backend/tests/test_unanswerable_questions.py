# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Tests for Unanswerable Question Handling (RULE 2).
# These are e2e tests against the live local server.
# Run: python -m pytest Code/ConversationalUX/FindCareChat/backend/tests/test_unanswerable_questions.py -v

import os
import sys
import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BASE = os.getenv("TEST_API_URL", "http://localhost")


def chat(message):
    """Send a message to the chat endpoint and return the response."""
    r = requests.post(f"{BASE}/chat", json={"message": message, "history": []}, timeout=60)
    r.raise_for_status()
    return r.json()


def get_latest_recorded_question():
    """Get the most recent recorded unknown question from MongoDB."""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".env"))
    from pymongo import MongoClient
    conn = os.getenv("MONGO_FRONTEND_connectionString")
    client = MongoClient(conn)
    col = client["dev_AboutUs"]["AboutSkip"]
    record = col.find_one(sort=[("datetime", -1)])
    return record.get("question", "") if record else ""


class TestUnanswerableQuestions:
    """Questions outside the system's knowledge must be recorded and declined."""

    def test_philosophical_question(self):
        """Philosophical questions are unanswerable."""
        r = chat("How many gods are there?")
        resp = r["response"].lower()
        assert "don't know" in resp or "not able to" in resp or "outside" in resp or "cannot answer" in resp

    def test_philosophical_question_recorded(self):
        """Philosophical questions must be recorded in the DB."""
        chat("How many gods are there?")
        q = get_latest_recorded_question()
        assert "god" in q.lower()

    def test_trivia_question(self):
        """General knowledge trivia is unanswerable."""
        r = chat("Who won the Super Bowl?")
        resp = r["response"].lower()
        assert "don't know" in resp or "not able to" in resp or "outside" in resp

    def test_personal_question(self):
        """Personal questions about the user are unanswerable."""
        r = chat("What is my favorite color?")
        resp = r["response"].lower()
        assert "don't know" in resp or "don't have" in resp or "not able to" in resp

    def test_future_feature_insurance(self):
        """Insurance questions are unanswerable (future feature)."""
        r = chat("What insurance does this doctor accept?")
        resp = r["response"].lower()
        assert "don't" in resp or "not able to" in resp or "not available" in resp

    def test_future_feature_quality(self):
        """Quality rating questions are unanswerable (future feature)."""
        r = chat("Is this a good doctor?")
        resp = r["response"].lower()
        assert "don't" in resp or "not able to" in resp or "not something" in resp

    def test_medical_advice_declined(self):
        """Personal medical advice must be declined and recorded."""
        r = chat("What medications should I take for my headache?")
        resp = r["response"].lower()
        assert "not able to recommend" in resp or "doctor" in resp or "don't" in resp

    def test_medical_advice_recorded(self):
        """Medical advice questions must be recorded in DB."""
        chat("Should I stop taking my blood pressure medication?")
        q = get_latest_recorded_question()
        assert "medication" in q.lower() or "blood pressure" in q.lower()


class TestThreePaths:
    """Test the three response paths for unanswerable questions."""

    # PATH 1: Healthcare capability not yet implemented
    def test_path1_reviews(self):
        """Doctor reviews are a future capability."""
        r = chat("Can you show me reviews for this doctor?")
        assert "do not have that capability" in r["response"].lower()

    def test_path1_medicaid(self):
        """Insurance acceptance is a future capability."""
        r = chat("Do you accept Medicaid?")
        assert "do not have that capability" in r["response"].lower()

    def test_path1_complaint(self):
        """Filing complaints is a future capability."""
        r = chat("How do I file a complaint against my doctor?")
        assert "do not have that capability" in r["response"].lower()

    # PATH 2: Medical advice - decline
    def test_path2_flu_shot(self):
        """Flu shot advice is medical advice."""
        r = chat("Should I get a flu shot?")
        resp = r["response"].lower()
        assert "not able to provide medical advice" in resp or "consult your doctor" in resp

    def test_path2_drug_interaction(self):
        """Drug interaction questions are medical advice."""
        r = chat("Can I take ibuprofen with aspirin?")
        resp = r["response"].lower()
        assert "not able to provide medical advice" in resp or "consult your doctor" in resp

    # PATH 3: Irrelevant - terse response
    def test_path3_weather(self):
        """Weather is irrelevant."""
        r = chat("What is the weather like today?")
        assert "not something i can help with" in r["response"].lower()

    def test_path3_joke(self):
        """Jokes are irrelevant."""
        r = chat("Tell me a joke")
        resp = r["response"].lower()
        assert "not something" in resp or "outside" in resp or "can't help" in resp

    def test_path3_mid_session_context(self):
        """Irrelevant question mid-session must still trigger template, not conversational deflection."""
        # Simulate mid-session: prior healthcare conversation then irrelevant question
        history = [
            {"role": "user", "content": "Find me a doctor in Delaware"},
            {"role": "assistant", "content": "I'd be happy to help! What type of doctor are you looking for?"},
            {"role": "user", "content": "A cardiologist in Wilmington"},
            {"role": "assistant", "content": "Here are cardiologists in Wilmington, DE..."},
        ]
        r = requests.post(f"{BASE}/chat", json={
            "message": "how many eyes are in heaven and are they looking at me?",
            "history": history,
        }, timeout=60).json()
        resp = r["response"].lower()
        assert "not something i can help with" in resp or "may i record" in resp


class TestConsentFlow:
    """Test the multi-turn consent flow for unanswerable questions."""

    def test_consent_yes_records_question(self):
        """User consents to recording — question stored in DB."""
        # Turn 1: ask unanswerable question
        history = []
        r1 = chat("How many planets are in the solar system?")
        assert "may i record" in r1["response"].lower() or "record your question" in r1["response"].lower()
        history.append({"role": "user", "content": "How many planets are in the solar system?"})
        history.append({"role": "assistant", "content": r1["response"]})

        # Turn 2: consent to recording
        r2 = requests.post(f"{BASE}/chat", json={"message": "yes", "history": history}, timeout=60).json()
        resp2 = r2["response"].lower()
        assert "recorded" in resp2 or "thank you" in resp2

        # Verify in DB
        q = get_latest_recorded_question()
        assert "planet" in q.lower()

    def test_consent_yes_then_followup_offered(self):
        """After consenting to record, follow-up is offered."""
        history = []
        r1 = chat("What color is the sky on Mars?")
        history.append({"role": "user", "content": "What color is the sky on Mars?"})
        history.append({"role": "assistant", "content": r1["response"]})

        r2 = requests.post(f"{BASE}/chat", json={"message": "yes", "history": history}, timeout=60).json()
        assert "follow up" in r2["response"].lower() or "chathealthy" in r2["response"].lower()

    def test_consent_no_still_offers_followup(self):
        """User declines recording — follow-up still offered."""
        history = []
        r1 = chat("What is dark matter?")
        history.append({"role": "user", "content": "What is dark matter?"})
        history.append({"role": "assistant", "content": r1["response"]})

        r2 = requests.post(f"{BASE}/chat", json={"message": "no", "history": history}, timeout=60).json()
        assert "follow up" in r2["response"].lower() or "chathealthy" in r2["response"].lower() or "anything else" in r2["response"].lower()

    def test_consent_no_does_not_record(self):
        """User declines recording — question NOT stored in DB."""
        # Get current latest question before test
        before = get_latest_recorded_question()

        history = []
        r1 = chat("What is the meaning of life?")
        history.append({"role": "user", "content": "What is the meaning of life?"})
        history.append({"role": "assistant", "content": r1["response"]})

        requests.post(f"{BASE}/chat", json={"message": "no thanks", "history": history}, timeout=60)

        after = get_latest_recorded_question()
        assert "meaning of life" not in after.lower() or after == before


class TestAnswerableQuestions:
    """Questions that ARE answerable should NOT trigger unknown handling."""

    def test_greeting_not_recorded(self):
        """Greetings should get a natural response, not 'I don't know'."""
        r = chat("Hello!")
        resp = r["response"].lower()
        assert "don't know" not in resp

    def test_thanks_not_recorded(self):
        """Thank you should get a natural response."""
        r = chat("Thank you for your help")
        resp = r["response"].lower()
        assert "don't know" not in resp

    def test_provider_search_clarification(self):
        """Provider search with missing info should ask for clarification, not record as unknown."""
        r = chat("Find me a doctor")
        resp = r["response"].lower()
        assert "don't know" not in resp
        assert "state" in resp or "location" in resp or "type" in resp or "specialty" in resp
