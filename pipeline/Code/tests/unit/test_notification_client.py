# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

from __future__ import annotations

import os

from notification_client import NotificationClient, twilio_enabled


def test_twilio_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TWILIO_ENABLED", raising=False)
    assert twilio_enabled() is False
    assert NotificationClient().send_sms("+1", "hi")["status"] == "noop"


def test_twilio_enabled_still_no_live_send(monkeypatch):
    monkeypatch.setenv("TWILIO_ENABLED", "1")
    # Tests MUST NEVER hit a live email endpoint. Clear every credential
    # env var that would cause NotificationClient to attempt a live send
    # (conftest.py loads .env at collection time so these may be
    # populated with real keys in the test process's env).
    monkeypatch.delenv("SPARKMAIL_API_KEY", raising=False)
    monkeypatch.delenv("SPARKPOST_API_KEY", raising=False)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_API_KEY_SID", raising=False)
    monkeypatch.delenv("TWILIO_API_KEY_SECRET", raising=False)
    assert twilio_enabled() is True
    assert NotificationClient().send_email("a@b.com", "s", "b")["status"] == "noop"
