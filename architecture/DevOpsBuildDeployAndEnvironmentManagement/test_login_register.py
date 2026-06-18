# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Playwright tests for EPIC-002-F-003-S-004 Login & Registration.

Stub. Covers REQ-B-001 through REQ-B-019. Two canned email outcomes
drive the local-fake OAuth flow: Claude@anthropic.ai succeeds,
ClaudeCode@anthropic.ai fails. Real Google OAuth is bypassed on local.
"""
import pytest


@pytest.mark.skip(reason="stub — implementation pending OAuthLoginTool")
def test_login_register_success_new_user():
    pass


@pytest.mark.skip(reason="stub — implementation pending OAuthLoginTool")
def test_login_register_failure_unauthorized():
    pass


@pytest.mark.skip(reason="stub — implementation pending OAuthLoginTool")
def test_login_register_returning_user_welcome_back():
    pass
