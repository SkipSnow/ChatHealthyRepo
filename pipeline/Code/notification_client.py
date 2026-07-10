# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

import logging
import os
from typing import Any

_log = logging.getLogger("notification_client")


def twilio_enabled() -> bool:
    return os.environ.get("TWILIO_ENABLED", "").lower() in ("1", "true", "yes")


class NotificationClient:
    """Twilio dispatch — first-pass no-op unless TWILIO_ENABLED is set."""

    def __init__(self) -> None:
        self.enabled = twilio_enabled()

    def send_sms(self, to: str, body: str, **meta: Any) -> dict:
        if not self.enabled:
            _log.info("notification SMS (no-op) to=%s body=%s meta=%s", to, body[:120], meta)
            return {"channel": "sms", "status": "noop", "to": to}
        _log.warning("TWILIO_ENABLED set but Twilio client not wired — SMS skipped to=%s", to)
        return {"channel": "sms", "status": "skipped", "to": to}

    def send_email(self, to: str, subject: str, body: str, attachments: list | None = None, **meta: Any) -> dict:
        if not self.enabled:
            _log.info(
                "notification email (no-op) to=%s subject=%s attachments=%s",
                to, subject, [a.get("filename") for a in (attachments or [])],
            )
            return {"channel": "email", "status": "noop", "to": to}
        _log.warning("TWILIO_ENABLED set but email client not wired — email skipped to=%s", to)
        return {"channel": "email", "status": "skipped", "to": to}
