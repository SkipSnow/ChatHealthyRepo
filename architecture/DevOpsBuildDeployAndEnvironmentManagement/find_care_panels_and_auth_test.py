# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# find_care_panels_and_auth_test.py
# Comprehensive smoke covering every file touched during the
# display-logic-out-of-Python cleanup:
#   - AboutChatHealthyWidget (About popup + security panel)
#   - SharedServicesSplashWidget (identity + threads)
#   - EvaluateCareSplashWidget (structured-data render: title + subtitle,
#     no html field)
#   - HeaderWidget (banner replaces header on oauth result; close button
#     restores header)
#   - FakeGoogleLoginWidget (React-rendered sign-in form)
#   - OAuth end-to-end via fake_google: form → submit → popup closes via
#     ?oauth_popup_close=1 → HeaderWidget polling claims result → banner
#   - claim_oauth_result /gate op (read-once clear semantics)
#   - evalcare-splash /gate op (structured JSON, no html)
#
# Local-only — uses fake_google which only exists in the local stack.

import os
import pytest
from playwright.sync_api import sync_playwright

BASE_URL = os.getenv("SMOKE_TEST_URL", "https://localhost")
DEFAULT_TIMEOUT = 60_000

SCREENSHOT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "_oneshots", "test_output", "panels_auth_smoke",
)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


def _shot(page, name):
    page.screenshot(path=os.path.join(SCREENSHOT_DIR, f"{name}.png"), full_page=True)


# ── Browser fixture ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def env():
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=["--ignore-certificate-errors"])
    context = browser.new_context(ignore_https_errors=True)
    page = context.new_page()
    page.set_default_timeout(DEFAULT_TIMEOUT)
    page.goto(BASE_URL, wait_until="networkidle")
    page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
    # Wait for the header to be painted by HeaderWidget so subsequent
    # selectors don't race the iframe load.
    page.wait_for_selector("[data-router-action='about_chathealthy']",
                           timeout=DEFAULT_TIMEOUT)
    yield {"page": page, "context": context}
    context.close()
    browser.close()
    pw.stop()


# Direct /gate-op tests live in find_care_gate_ops_test.py (separate file
# so pymongo's imports cannot leave an asyncio loop running and break
# sync_playwright in this file's browser fixture).


# ── Browser smoke ────────────────────────────────────────────────────────

def _open_about(page):
    page.locator("[data-router-action='about_chathealthy']").first.click()
    # About popup mounts into #popup_AboutChatHealtyPopUP per ClientRouter
    page.wait_for_selector("text=Build", timeout=DEFAULT_TIMEOUT)


class TestAboutPopup:
    """AboutChatHealthyWidget — the popup renders the security panel
    (version/build/session token bits) from structured data only."""

    def test_about_popup_shows_build_facts(self, env):
        page = env["page"]
        _open_about(page)
        body = page.locator("body").inner_text()
        _shot(page, "about_popup")
        assert "Version" in body or "version" in body.lower(), \
            f"About panel must surface a Version label. body[:400]={body[:400]!r}"
        assert "Build" in body, f"About panel must surface a Build label. body[:400]={body[:400]!r}"

    def test_about_popup_shows_security_panel(self, env):
        page = env["page"]
        _open_about(page)
        body = page.locator("body").inner_text().lower()
        # The widget labels the SessionToken parts as Authorization ID,
        # Nonce, and Session GUID per the operator-approved labels.
        for label in ("authorization id", "nonce", "session guid"):
            assert label in body, (
                f"About panel must include security label {label!r}. "
                f"body[:500]={body[:500]!r}"
            )


class TestSharedServicesSplash:
    """SharedServicesSplashWidget — clicking the SS nav button in the
    About popup paints the user_object identity + threads grid into
    frame_MainWindow."""

    def test_shared_services_splash_renders_identity_and_threads(self, env):
        page = env["page"]
        _open_about(page)
        page.locator("button[data-router-action='goto_sharedservices']").first.click()
        # MainWindow now hosts the splash
        page.wait_for_selector("text=Shared Services", timeout=DEFAULT_TIMEOUT)
        body = page.locator("#frame_MainWindow").inner_text()
        lower = body.lower()
        _shot(page, "shared_services_splash")
        assert "identity" in lower, (
            f"SharedServices splash must render the Identity section. "
            f"MainWindow[:400]={body[:400]!r}"
        )
        assert "utterances" in lower and "actions" in lower, (
            f"SharedServices splash must render both threads. "
            f"MainWindow[:400]={body[:400]!r}"
        )


class TestEvaluateCareSplash:
    """EvaluateCareSplashWidget — receives structured JSON
    {title, subtitle} from the tool. No html string anywhere."""

    def test_evaluate_care_splash_renders_from_structured_data(self, env):
        page = env["page"]
        _open_about(page)
        page.locator("button[data-router-action='goto_evaluatecare']").first.click()
        # Wait for the structured-data render (subtitle present) — not
        # the loading-state intermediate ("Loading EvaluateCare…") which
        # also contains the word "EvaluateCare".
        page.wait_for_function(
            """() => {
                const m = document.getElementById('frame_MainWindow');
                const t = (m && m.innerText || '').toLowerCase();
                return t.indexOf('unimplemented') !== -1;
            }""",
            timeout=DEFAULT_TIMEOUT,
        )
        body = page.locator("#frame_MainWindow").inner_text()
        _shot(page, "evalcare_splash")
        assert "EvaluateCare" in body, (
            f"EvaluateCare main window must render the title. "
            f"MainWindow[:400]={body[:400]!r}"
        )
        assert "unimplemented" in body.lower(), (
            f"EvaluateCare main window must render the subtitle. "
            f"MainWindow[:400]={body[:400]!r}"
        )


# ── Full OAuth end-to-end via fake_google ────────────────────────────────

class TestOAuthFlowViaFakeGoogle:
    """Click Login & Registration → popup opens → React-rendered fake
    sign-in form → submit credentials → callback redirects popup with
    ?oauth_popup_close=1 → wrapper closes itself → HeaderWidget polling
    claims pending_oauth_result → banner replaces normal header → close
    button restores the normal header.

    Verifies every file touched: HeaderWidget, OAuthLoginWidget,
    FakeGoogleLoginWidget, fake_google_endpoint, google_oauth_endpoint,
    universal_navigation_tool (claim_oauth_result), index.html
    (popup_close + popup_params forwarding), ClientRouter.js."""

    def test_oauth_end_to_end(self, env):
        # OAuth needs a clean page: prior tests' router state (subscriptions,
        # in-flight broadcasts, repainted MainWindow) racing against the
        # post-callback polling produced flaky banner-paint failures. Open
        # a fresh tab so this test owns its session top-to-bottom.
        context = env["context"]
        page = context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT)
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_selector("[data-router-action='about_chathealthy']",
                               timeout=DEFAULT_TIMEOUT)
        # Wait for ClientRouter to capture the session GUID from the boot
        # stream. If the GUID is still empty when oauth_start fires, the
        # OAuth form posts session_guid='' and the server mints a random
        # one — the callback then persists on the wrong session and
        # HeaderWidget's polling reads from the main page's session (which
        # is the empty/null case) and never sees the result.
        page.wait_for_function(
            """() => {
                return !!(window.ClientRouter &&
                          typeof window.ClientRouter.getSessionGuid === 'function' &&
                          window.ClientRouter.getSessionGuid() &&
                          window.ClientRouter.getSessionGuid().length === 32);
            }""",
            timeout=DEFAULT_TIMEOUT,
        )

        # Click Login → opens a popup
        with context.expect_page(timeout=DEFAULT_TIMEOUT) as popup_info:
            page.locator("button[data-router-action='oauth_start']").first.click()
        popup = popup_info.value
        popup.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT)

        # FakeGoogleLoginWidget renders the form into popup's MainWindow.
        popup.wait_for_selector(
            "input[data-testid='fake-google-email']", timeout=DEFAULT_TIMEOUT,
        )
        _shot(popup, "popup_fake_login_form")
        popup.locator("input[data-testid='fake-google-email']").fill("oauthtest@example.com")
        popup.locator("input[data-testid='fake-google-password']").fill("notreallychecked")

        # Submitting the form navigates the popup through /fake_google/submit
        # → /auth/google/callback → 302 to wrapper with ?oauth_popup_close=1
        # → wrapper's index.html calls window.close(). The popup closes.
        popup.locator("button[data-testid='fake-google-submit-button']").click()
        try:
            popup.wait_for_event("close", timeout=DEFAULT_TIMEOUT)
        except Exception:
            try:
                final_url = popup.url
                _shot(popup, "popup_did_not_close")
            except Exception:
                final_url = "<unreadable>"
            pytest.fail(
                f"OAuth popup did not auto-close. final_url={final_url!r}. "
                f"The ?oauth_popup_close=1 branch in index.html may not be "
                f"firing, or the callback redirected somewhere else."
            )

        # Brief settle window so HeaderWidget's first post-close poll
        # cycle completes before we start watching frame_Header. The
        # widget polls at 1 Hz; the popup-close event arrives a few ms
        # before the next poll fires.
        page.wait_for_timeout(1200)

        # HeaderWidget polls /gate op=claim_oauth_result every second.
        # The callback persisted pending_oauth_result on the user_object;
        # the next poll should claim it and the banner repaints the
        # header. We watch for the banner text in frame_Header.
        try:
            page.wait_for_function(
                """() => {
                    const h = document.getElementById('frame_Header');
                    if (!h) return false;
                    const t = (h.innerText || '').toLowerCase();
                    // banner text contains 'oauthtest' (the email we used) OR
                    // the canonical welcome phrase from OAuthLoginTool
                    return t.indexOf('oauthtest') !== -1 || t.indexOf('welcome') !== -1;
                }""",
                timeout=60_000,
            )
        except Exception:
            try:
                header_text = page.locator("#frame_Header").inner_text()
                _shot(page, "main_header_banner_missing")
            except Exception:
                header_text = "<unreadable>"
            pytest.fail(
                f"OAuth banner did not appear in 60s. "
                f"frame_Header text={header_text!r}"
            )
        _shot(page, "main_header_banner")

        # Close button in the banner restores the normal header
        page.locator("#frame_Header button[data-router-action='oauth_banner_close']").first.click()
        page.wait_for_selector(
            "#frame_Header button[data-router-action='oauth_start']",
            timeout=DEFAULT_TIMEOUT,
        )
        _shot(page, "main_header_restored")
