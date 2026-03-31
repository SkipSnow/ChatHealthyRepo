# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# AboutService — UAT Feature 4: About ChatHealthy / Skip Snow
#
# Design: ARCH-001, shared infrastructure

import logging

_log = logging.getLogger("findcare.about")


class AboutService:
    """Static content for about intents.

    Dependencies: pre-loaded context dict (ME).
    """

    def __init__(self, me_context: dict, trim_fn=None):
        self._me = me_context
        self._trim = trim_fn or (lambda text, n: text[:n])

    def get_skip_snow_context(self) -> dict:
        """Return Skip Snow's professional context."""
        return {
            "summary": self._me.get("summary", ""),
            "linkedin": self._trim(self._me.get("linkedin", ""), 3000),
        }

    def get_chathealthy_context(self) -> dict:
        """Return ChatHealthy.AI company context."""
        return {
            "business_plan": self._trim(self._me.get("business_plan", ""), 3000),
            "anthropic_principles": self._me.get("anthropic_principles", ""),
        }
