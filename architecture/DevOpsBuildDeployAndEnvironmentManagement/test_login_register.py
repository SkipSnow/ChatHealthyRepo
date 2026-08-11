# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Playwright tests for EPIC-002-F-003-S-004 Login & Registration.

Drives the live Login & Registration nav button only. Server returns
the popup destination via 302; popup is Google (real) on dev/qa/prod
or our fake page on local. Wrapper has no env-conditional code.

Tests:
  1 (B-013): Claude@anthropic.ai new user -> success banner with welcome.
  2 (B-016): ClaudeCode@anthropic.ai not on allow-list -> fail banner.
  3 (B-017): Claude@anthropic.ai returning -> welcome back + timestamp.
  4 (B-020): skip.snow@gmail.com not on allow-list -> fail banner.
  5 (B-022): pre-existing cookie + failed auth -> cookie preserved,
             user_object preserved, banner does not leak prior state.
"""
import os

import pytest
from playwright.sync_api import sync_playwright, expect


# Rule-004: one place in this file obtains a connection, and it goes through
# the canonical utility. The certificate is the credential; there is no
# connection string here and no fallback. Raises if the identity cannot
# connect, which is the point -- a test that quietly connects as something
# else proves nothing about production.
def _ch_connection():
    import sys as _sys, pathlib as _pl
    for _d in _pl.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _lib = _d / "ChatHealthyLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            break
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    return ChatHealthyMongoUtilities().getConnection("DevOpsUser", 'frontEnd')


BASE_URL = os.getenv("LOGIN_TEST_URL", "https://localhost")
TEST_EMAILS = [
    "Claude@anthropic.ai", "ClaudeCode@anthropic.ai", "rejected.test@example.com",
]
COOKIE_TEST_USER_ID = "u-test-b022-prealpha-returner"
COOKIE_TEST_EMAIL = "returner.b022@example.com"


@pytest.fixture(scope="module")
def mongo_frontend():
    from pymongo import MongoClient
    from dotenv import load_dotenv
    load_dotenv("Code/.env")
    mc = _ch_connection()
    yield mc
    mc.close()


@pytest.fixture(scope="module", autouse=True)
def reset_canned_test_users(mongo_frontend):
    db = mongo_frontend["Users"]
    emails = TEST_EMAILS + [COOKIE_TEST_EMAIL]
    db["users"].delete_many({
        "user_object.OAuthIdentities.email": {"$in": emails},
    })
    db["users"].delete_many({"user_id": COOKIE_TEST_USER_ID})
    db["sessions"].delete_many({
        "OAuthIdentities.email": {"$in": emails},
    })
    yield


@pytest.fixture(scope="module")
def browser_context():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            ignore_https_errors=True,
        )
        yield context
        context.close()
        browser.close()


def _drive_login_via_nav_button(context, email, register=False):
    page = context.new_page()
    page.goto(BASE_URL, wait_until="networkidle")
    nav_button = page.locator(
        'button:has-text("Login & Registration")'
    ).first
    expect(nav_button).to_be_visible(timeout=10000)
    with page.expect_popup() as popup_info:
        nav_button.click()
    popup = popup_info.value
    popup.wait_for_load_state("networkidle", timeout=20000)
    popup.locator('[data-testid="fake-google-email"]').fill(email)
    popup.locator('[data-testid="fake-google-password"]').fill("test-password")
    if register:
        cb = popup.locator('[data-testid="fake-google-register-checkbox"]')
        if not cb.is_checked():
            cb.check()
    popup.locator('[data-testid="fake-google-submit-button"]').click()
    popup.wait_for_event("close", timeout=15000)
    banner = page.locator("#chAuthBanner")
    expect(banner).to_be_visible(timeout=10000)
    return page, banner.inner_text()


class TestStep01OAuthClaudeNewUserSuccess:
    """REQ-B-013: Claude@anthropic.ai first login -> new pre-alpha welcome."""

    def test_claude_success(self, browser_context):
        page, text = _drive_login_via_nav_button(
            browser_context, "Claude@anthropic.ai", register=True,
        )
        assert "logged in" in text.lower(), f"missing welcome: {text[:300]}"
        assert "pre-alpha" in text.lower(), f"missing pre-alpha: {text[:300]}"
        assert "Claude@anthropic.ai" in text, f"missing email: {text[:300]}"
        page.close()


class TestStep02OAuthClaudeCodeFail:
    """REQ-B-016: ClaudeCode@anthropic.ai not on allow-list -> fail."""

    def test_claudecode_fail(self, browser_context):
        page, text = _drive_login_via_nav_button(
            browser_context, "ClaudeCode@anthropic.ai",
        )
        assert "pre-alpha" in text.lower(), f"missing explainer: {text[:300]}"
        assert "skip@chathealthy.ai" in text.lower(), f"missing email: {text[:300]}"
        assert "646" in text, f"missing phone: {text[:300]}"
        page.close()


class TestStep03OAuthClaudeReturning:
    """REQ-B-017: Claude@anthropic.ai second login -> Welcome back + last."""

    def test_claude_returning(self, browser_context):
        page, text = _drive_login_via_nav_button(
            browser_context, "Claude@anthropic.ai",
        )
        assert "welcome back" in text.lower(), f"missing welcome-back: {text[:300]}"
        assert "last logged in" in text.lower(), f"missing last-login: {text[:300]}"
        page.close()


class TestStep04OAuthThirdRejectionFail:
    """REQ-B-020 + B-016: a third non-allow-listed email fails the same way."""

    def test_third_rejection_fail(self, browser_context):
        page, text = _drive_login_via_nav_button(
            browser_context, "rejected.test@example.com",
        )
        assert "pre-alpha" in text.lower(), f"missing explainer: {text[:300]}"
        assert "skip@chathealthy.ai" in text.lower(), f"missing email: {text[:300]}"
        assert "646" in text, f"missing phone: {text[:300]}"
        page.close()


class TestStep05B022FailedAuthPreservesCookieAndUserObject:
    """REQ-B-022: prior cookie + failed auth -> cookie preserved,
    user_object preserved, message does not leak prior state."""

    def test_failed_auth_preserves_state(self, browser_context, mongo_frontend):
        from datetime import datetime, timezone
        users = mongo_frontend["Users"]["users"]
        now_iso = datetime.now(timezone.utc).isoformat()
        users.insert_one({
            "user_id": COOKIE_TEST_USER_ID,
            "user_object": {
                "user_id": COOKIE_TEST_USER_ID,
                "is_registered": True,
                "user_type": "Prospect",
                "public_username": COOKIE_TEST_EMAIL,
                "OAuthIdentities": [{
                    "identity_provider": "Google",
                    "identity_provider_user_id": "999999999999999999999",
                    "email": COOKIE_TEST_EMAIL,
                }],
                "first_login_at": now_iso,
                "last_login_at": now_iso,
            },
        })
        browser_context.add_cookies([{
            "name": "ChatHealthyRegisteredUserCookie",
            "value": COOKIE_TEST_USER_ID,
            "domain": "localhost",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }])

        page, text = _drive_login_via_nav_button(
            browser_context, COOKIE_TEST_EMAIL,
        )

        lower = text.lower()
        assert "pre-alpha" in lower, f"missing fail explainer: {text[:300]}"
        for leak in ["cookie", "previously", "prior", "registered before",
                     "last logged"]:
            assert leak not in lower, (
                f"banner leaked prior-state token '{leak}': {text[:300]}"
            )

        cookies_now = {c["name"]: c["value"] for c in browser_context.cookies()}
        assert cookies_now.get("ChatHealthyRegisteredUserCookie") == (
            COOKIE_TEST_USER_ID
        ), f"cookie was modified or removed: {cookies_now}"

        doc = users.find_one({"user_id": COOKIE_TEST_USER_ID})
        assert doc is not None, "user_object was deleted on failed re-auth"

        page.close()
