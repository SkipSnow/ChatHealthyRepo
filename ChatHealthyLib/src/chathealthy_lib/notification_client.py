# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Pipeline operator notification substrate.

Two channels:
  * email  — SparkPost REST API, enabled when SPARKMAIL_API_KEY is set.
             Same substrate the reservation_reaper uses for its hourly
             streak alerts.
  * sms    — Twilio, enabled when TWILIO_ENABLED is truthy AND the
             three Twilio env vars are set. First-pass no-op if either
             gate is off.

Both channels are best-effort: a failure to dispatch logs at warning
level and returns a status dict; the caller never crashes on a
notification failure.
"""

from __future__ import annotations

import base64 as _b64
import os
from typing import Any

import requests

from chathealthy_lib.logging_service import ChatHealthyLoggingService

_log = ChatHealthyLoggingService()


def twilio_enabled() -> bool:
    return os.environ.get("TWILIO_ENABLED", "").lower() in ("1", "true", "yes")


def sparkpost_enabled() -> bool:
    return bool(os.environ.get("SPARKMAIL_API_KEY", "").strip())


class NotificationClient:
    """Operator notification client for pipeline events."""

    def __init__(self) -> None:
        self.sparkpost_enabled = sparkpost_enabled()
        self.twilio_enabled = twilio_enabled()

    def send_sms(self, to: str, body: str, **meta: Any) -> dict:
        if not self.twilio_enabled:
            _log.info("notification SMS (no-op) to=%s body=%s meta=%s",
                      to, body[:120], meta)
            return {"channel": "sms", "status": "noop", "to": to}
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
        api_key_sid = os.environ.get("TWILIO_API_KEY_SID", "").strip()
        api_key_secret = os.environ.get("TWILIO_API_KEY_SECRET", "").strip()
        # Twilio supports two auth patterns. Prefer API-key pair (more
        # granular + revocable independently); fall back to legacy
        # Account SID + Auth Token. URL always uses Account SID.
        if api_key_sid and api_key_secret:
            auth_username, auth_password = api_key_sid, api_key_secret
        else:
            auth_username = account_sid
            auth_password = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
        frm = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
        if not (account_sid and auth_username and auth_password and frm):
            _log.warning(
                "TWILIO_ENABLED but ACCOUNT_SID/auth/FROM missing; SMS skipped to=%s", to,
            )
            return {"channel": "sms", "status": "misconfigured", "to": to}
        try:
            r = requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
                auth=(auth_username, auth_password),
                data={"From": frm, "To": to, "Body": body},
                timeout=15,
            )
            if r.status_code in (200, 201):
                _log.info("notification SMS delivered to=%s sid=%s",
                          to, r.json().get("sid", ""))
                return {"channel": "sms", "status": "delivered", "to": to}
            _log.warning("Twilio SMS failed status=%s body=%s to=%s",
                         r.status_code, r.text[:300], to)
            return {"channel": "sms", "status": "twilio_error", "to": to,
                    "http_status": r.status_code}
        except Exception as exc:  # noqa: BLE001
            _log.warning("Twilio SMS exception to=%s err=%s",
                         to, str(exc)[:300])
            return {"channel": "sms", "status": "exception", "to": to,
                    "error": str(exc)[:300]}

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: list | None = None,
        html_body: str | None = None,
        log_context: str = "",
        **meta: Any,
    ) -> dict:
        # Auto-promote HTML-looking body to html_body so Gmail renders
        # the table instead of showing raw <html> markup.
        if html_body is None and body.lstrip().lower().startswith(
            ("<html", "<body", "<!doctype")
        ):
            html_body = body
            body = "See HTML version of this notification."
        if not self.sparkpost_enabled:
            _log.info(
                "notification email (no-op) to=%s subject=%s attachments=%s",
                to, subject, [a.get("filename") for a in (attachments or [])],
            )
            return {"channel": "email", "status": "noop", "to": to}
        api_key = os.environ["SPARKMAIL_API_KEY"].strip()
        from_addr = os.environ.get(
            "NOTIFICATION_FROM_EMAIL", "noreply@chathealthy.ai"
        ).strip()
        sp_attachments = []
        for a in attachments or []:
            content = a.get("content", b"")
            if isinstance(content, str):
                content = content.encode("utf-8")
            sp_attachments.append({
                "type": a.get("type", "application/pdf"),
                "name": a.get("filename", "attachment"),
                "data": _b64.b64encode(content).decode("ascii"),
            })
        content = {
            "from": from_addr,
            "subject": subject,
            "text": body,
        }
        if html_body:
            content["html"] = html_body
        payload = {"content": content, "recipients": [{"address": to}]}
        if sp_attachments:
            payload["content"]["attachments"] = sp_attachments
        try:
            r = requests.post(
                "https://api.sparkpost.com/api/v1/transmissions",
                headers={"Authorization": api_key,
                         "Content-Type": "application/json"},
                json=payload,
                timeout=15,
            )
            if r.status_code in (200, 201, 202):
                _log.info("notification email delivered to=%s subject=%s%s",
                          to, subject[:120],
                          f" {log_context}" if log_context else "")
                return {"channel": "email", "status": "delivered", "to": to}
            _log.warning("SparkPost email failed status=%s body=%s to=%s",
                         r.status_code, r.text[:300], to)
            return {"channel": "email", "status": "sparkpost_error", "to": to,
                    "http_status": r.status_code}
        except Exception as exc:  # noqa: BLE001
            _log.warning("SparkPost email exception to=%s err=%s",
                         to, str(exc)[:300])
            return {"channel": "email", "status": "exception", "to": to,
                    "error": str(exc)[:300]}
