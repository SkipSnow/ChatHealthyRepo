# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# LeadService — UAT Feature 6: Lead Capture (follow-up offer)
#
# Delegates to ConsentService for data handling per consent tier.
#
# Design: ARCH-001, shared infrastructure

from chathealthy_frontend_lib import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException
from datetime import datetime

from domain.shared.consent.consent_service import ConsentService

log = ChatHealthyLoggingService()


class LeadService:
    """Contact record creation with consent integration.

    Dependencies: MongoDB, ConsentService, push notification fn.
    """

    def __init__(self, get_db_fn, env_prefix: str, consent: ConsentService,
                 push_fn=None, commit_fn=None):
        self._get_db = get_db_fn
        self._env = env_prefix
        self._consent = consent
        self._push = push_fn or (lambda msg: None)
        self._commit = commit_fn or (lambda payload: None)

    def record_user_details(self, email="", name="Name not provided", notes="not provided",
                            message="", chat_history=None, consent_verbatim=False,
                            consent_summary=None, testdata=False) -> dict:
        """Record user contact details with consent-governed storage."""
        if not email or not str(email).strip():
            return {"recorded": "ok", "note": "Email required but not provided"}

        db = self._get_db()
        if db is None:
            self._push(f"Recording interest from {name} with email {email} (DB unavailable)")
            return {"recorded": "ok", "note": "MongoDB unavailable"}

        reason = message or notes
        lead_coll = db[f"{self._env}_AboutUs"]["lead"]

        try:
            for doc in lead_coll.find({"email": str(email).strip()}):
                return {"recorded": "ok"}
        except Exception as exc:
            log.error("record_user_details DB lookup failed: %s", exc, exc=ChatHealthyException(
                                                                        mode="lead_lookup_failed",
                                                                        message=f"record_user_details DB lookup failed: {exc}",
                                                                        component="LeadService",
                                                                        exception=exc,
                                                                    ), if_not_debug_log=True)
            return {"recorded": "error", "note": "Database error"}

        self._push(f"Recording interest from {name} with email {email}: {reason}")

        record = {
            "email": email, "name": name, "notes": notes,
            "reason_for_contact": reason,
            "consent_verbatim": consent_verbatim,
            "consent_summary": consent_summary,
            "datetime": datetime.now().isoformat(),
            "testdata": testdata,
        }

        if consent_verbatim:
            record["chat_history"] = chat_history or []
        elif consent_summary:
            summary = self._consent.summarize_conversation(chat_history)
            summary_msg = [{"role": "user", "content": summary}]
            self._consent.de_identify(summary_msg)
            record["notes"] = summary_msg[0]["content"]

        self._commit({"database": "AboutUs", "collection": "lead", "record": record})
        return {"recorded": "ok"}
