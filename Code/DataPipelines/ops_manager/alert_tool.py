# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# AlertTool — sends notifications via Pushover and SparkPost.
# Enforces cooldown windows per event type per subject.

import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "Shared"))
from agent_framework.base_tool import BaseTool, ToolResult

_log = logging.getLogger("ops.alert_tool")

# Cooldown windows in minutes: event_type -> minutes
COOLDOWNS = {
    "job_overdue": 60,
    "cluster_stuck": 30,
    "error_detected": 0,       # always immediate
    "self_repaired": 0,
    "dev_resolved": 0,
    "escalation": 0,
    "suspiciously_fast": 0,
}


class AlertTool(BaseTool):
    """Send notifications (Pushover + email). Enforces cooldown windows."""

    def __init__(self, push_fn=None, email_fn=None):
        self._push = push_fn
        self._email = email_fn
        self._last_sent: dict[str, datetime] = {}  # "event_type:subject" -> last sent

    @property
    def name(self) -> str:
        return "alert"

    @property
    def description(self) -> str:
        return "Send notifications to human or Dev Manager. Enforces cooldown windows."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "event_type": {
                    "type": "string",
                    "enum": list(COOLDOWNS.keys()),
                },
                "title": {"type": "string"},
                "message": {"type": "string"},
                "subject_id": {"type": "string", "description": "job_id or cluster_name for cooldown tracking"},
                "recipient": {"type": "string", "enum": ["human", "dev"], "default": "human"},
            },
            "required": ["event_type", "title", "message"],
        }

    def execute(self, **params) -> ToolResult:
        event_type = params["event_type"]
        title = params["title"]
        message = params["message"]
        subject_id = params.get("subject_id", "")
        recipient = params.get("recipient", "human")

        # Cooldown check
        cooldown_key = f"{event_type}:{subject_id}"
        cooldown_minutes = COOLDOWNS.get(event_type, 0)

        if cooldown_minutes > 0 and cooldown_key in self._last_sent:
            elapsed = (datetime.now(timezone.utc) - self._last_sent[cooldown_key]).total_seconds() / 60
            if elapsed < cooldown_minutes:
                _log.info("Cooldown: %s suppressed (%.0f min remaining)", cooldown_key, cooldown_minutes - elapsed)
                return ToolResult(success=True, data={"sent": False, "reason": "cooldown",
                                                       "remaining_minutes": round(cooldown_minutes - elapsed)})

        # Send
        sent_push = False
        sent_email = False

        if self._push:
            try:
                prefix = "[Dev] " if recipient == "dev" else ""
                self._push(f"{prefix}{title}", message)
                sent_push = True
            except Exception as e:
                _log.error("Pushover failed: %s", e)

        if self._email:
            try:
                prefix = "[Dev Manager] " if recipient == "dev" else "[Ops Manager] "
                self._email(f"{prefix}{title}", message)
                sent_email = True
            except Exception as e:
                _log.error("Email failed: %s", e)

        # Update cooldown tracker
        self._last_sent[cooldown_key] = datetime.now(timezone.utc)

        _log.info("Alert sent [%s -> %s]: %s", event_type, recipient, title)
        return ToolResult(success=True, data={
            "sent": True,
            "push": sent_push,
            "email": sent_email,
            "event_type": event_type,
            "recipient": recipient,
        })
