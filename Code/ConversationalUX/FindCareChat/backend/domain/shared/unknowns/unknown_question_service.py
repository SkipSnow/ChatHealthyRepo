# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# UnknownQuestionService — UAT Feature 12: Unanswerable Question Handling
#
# Design: ARCH-001, shared infrastructure

import logging

from domain.shared.consent.consent_service import ConsentService

_log = logging.getLogger("findcare.unknowns")

TEMPLATES = {
    "healthcare_capability": (
        'I don\'t have that capability yet. '
        'May I record your question so we can improve? '
        'We would save a de-identified version of this conversation.'
    ),
    "medical_advice": (
        'I am not able to provide medical advice. Please consult your doctor. '
        'May I record your question so we can improve? '
        'We would save a de-identified version of this conversation.'
    ),
    "irrelevant": (
        'That is not something I can help with. '
        'May I record your question so we can improve? '
        'We would save a de-identified version of this conversation.'
    ),
    # TODO: These templates need content management — not engineer decisions.
    # Backlog: load from brain or MongoDB, reviewed by human + GPT.
}


class UnknownQuestionService:
    """Classify and optionally record unanswerable questions.

    Dependencies: ConsentService (for de-identification), push/commit fns.
    """

    def __init__(self, consent: ConsentService, push_fn=None, commit_fn=None):
        self._consent = consent
        self._push = push_fn or (lambda msg: None)
        self._commit = commit_fn or (lambda payload: None)

    def record(self, question: str, question_class: str = "irrelevant",
               consent: bool = False, chat_history=None) -> dict:
        """Classify and optionally record an unanswerable question."""
        template = TEMPLATES.get(question_class, TEMPLATES["irrelevant"])

        if consent:
            if chat_history is not None:
                self._consent.de_identify(chat_history)
            self._push(f"Recording unanswerable question: {question}")
            self._commit({
                "database": "AboutUs",
                "collection": "AboutSkip",
                "record": {
                    "question": question,
                    "question_class": question_class,
                    "chat_history": chat_history or [],
                },
            })
            return {
                "recorded": "ok",
                "response_template": "Thank you, your question has been recorded. "
                "Would you like someone from the ChatHealthy team to follow up with you on this?",
            }

        return {
            "recorded": "pending_consent",
            "response_template": template,
            "question_class": question_class,
            "instruction": "Present the template VERBATIM. If user consents, call again with consent=true. "
            "If user declines recording, still ask: Would you like someone from ChatHealthy to follow up?",
        }
