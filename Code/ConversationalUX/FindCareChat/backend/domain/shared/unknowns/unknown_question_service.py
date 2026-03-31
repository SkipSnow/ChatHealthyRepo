# Copyright (c) 2026 Skip Snow. All rights reserved.
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
        'That\'s a great question, and it\'s something we\'re working on. '
        'ChatHealthy is focused on helping you find providers, specialties, and clinical trials right now, '
        'but we\'re expanding. May I record your question so we can prioritize it? '
        'We would save a de-identified version only.'
    ),
    "medical_advice": (
        'I appreciate you trusting me with that, but I\'m not able to provide medical advice. '
        'For personalized guidance, please consult your doctor or healthcare provider. '
        'I can help you find a specialist if you\'d like. '
        'May I record your question so we can improve? We would save a de-identified version only.'
    ),
    "irrelevant": (
        'That\'s outside what I can help with today. I\'m focused on healthcare navigation \u2014 '
        'finding providers, specialties, and clinical trials. '
        'May I record your question so we can learn from it? We would save a de-identified version only.'
    ),
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
