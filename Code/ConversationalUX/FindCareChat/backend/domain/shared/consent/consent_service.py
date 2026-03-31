# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ConsentService — UAT Feature 7: Consent Framework (two-tier HIPAA)
#
# This is a first-class bounded context, not a helper inside LeadService.
# Growth trajectory: revocation, granular consent, audit trail, multi-jurisdiction.
#
# Design: ARCH-001, shared infrastructure (bounded context)

import json
import logging
import os

_log = logging.getLogger("findcare.consent")


class ConsentService:
    """Two-tier HIPAA consent workflow.

    Tier 1: Verbatim transcript consent
    Tier 2: De-identified summary consent
    Neither: contact fields only
    """

    def __init__(self, anthropic_api_key: str = ""):
        self._anthropic_key = anthropic_api_key or os.getenv("Anthropic_API_KEY", "")

    def summarize_conversation(self, chat_history: list) -> str:
        """Summarize a conversation for Tier 2 consent."""
        if not chat_history:
            return ""
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=self._anthropic_key)
            chat_json = json.dumps(
                [{"role": m.get("role", ""), "content": m.get("content") or ""} for m in chat_history],
                indent=2,
            )
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content":
                    "Summarize this conversation in 2-3 sentences. Focus on what the user wanted "
                    "and any key information they shared. Be concise.\n\n" + chat_json}],
            )
            return response.content[0].text.strip()
        except Exception as exc:
            _log.error("summarize_conversation failed: %s", exc)
            return ""

    def de_identify(self, chat_history: list) -> None:
        """Strip PII from chat history in-place (HIPAA Safe Harbor).

        Modifies the list in place — each message's content is replaced with
        the de-identified version.
        """
        if not chat_history:
            return
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=self._anthropic_key)
            chat_json = json.dumps(
                [{"role": m.get("role", ""), "content": m.get("content") or ""} for m in chat_history],
                indent=2,
            )
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                messages=[{"role": "user", "content": (
                    "Deidentify the following chat conversation so it meets HIPAA Safe Harbor requirements.\n"
                    "Remove or replace: names, geographic identifiers (except state), dates (except year), "
                    "phone/fax, email, SSN, medical record numbers, account numbers, license numbers, "
                    "vehicle identifiers, device identifiers, URLs, IP addresses, and any other identifiers.\n"
                    "Preserve semantic meaning for research purposes.\n\n"
                    "Return ONLY a valid JSON array of strings, one per message in same order.\n\n"
                    f"Chat conversation:\n{chat_json}"
                )}],
            )
            result_text = response.content[0].text.strip()
            if result_text.startswith("```"):
                result_text = result_text.split("```")[1]
                if result_text.startswith("json"):
                    result_text = result_text[4:]
                result_text = result_text.strip()
            deidentified = json.loads(result_text)
            for i, content in enumerate(deidentified):
                if i < len(chat_history):
                    chat_history[i]["content"] = content
        except Exception as exc:
            _log.warning("de_identify failed: %s — keeping original", exc)
