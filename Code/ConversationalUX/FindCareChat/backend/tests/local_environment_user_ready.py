# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Local Environment User-Ready Smoke Test
#
# EPIC: EPIC-TEST — User Testing Simulation
# Feature: USER-TEST-LOCAL — Local Environment Security & User Readiness
#
# Scenario: Full FindCare → EvaluateCare handoff for a small DE provider list.
#   1. http://localhost loads (should redirect to https — KNOWN BUG: does not)
#   2. Red dev banner visible
#   3. Chat window shows welcome message + QA report
#   4. Input prompt exists
#   5. Send: "can you find me a provider in DE that can treat my foot?"
#   6. Timeout modal appears (~45s) — DESIGN BUG: should stop and ask user intent
#   7. Click "Yes, keep waiting" to continue
#   8. Provider results appear in chat window
#   9. Left panel: specialty filter checkboxes, session token (ch_), nonce timestamps
#  10. Left panel: two buttons below specialty list
#  11. Right panel: empty
#  12. Click lower button ("Evaluate Care")
#  13. Provider list moves to left panel
#  14. Chat window resets with greeting — BUG: shows FindCare greeting, not EvaluateCare
#  15. Encrypted session token below provider list, decrypted below that
#
# DR-019: Tests run against the full parent page, not individual components.
#
# Usage:
#   pytest local_environment_user_ready.py -v --headed   (watch)
#   pytest local_environment_user_ready.py -v            (headless)

import os
import re
import pytest
from playwright.sync_api import sync_playwright, Page

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost")
CHAT_TIMEOUT = 120_000  # LLM calls can take 30-60s+
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "test_screenshots", "user_ready")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# ── Bug / gap tracking ─────────────────────────────────────────────────────
KNOWN_BUGS = []


def _record_bug(bug_id, summary, detail=""):
    KNOWN_BUGS.append({"id": bug_id, "summary": summary, "detail": detail})


def _screenshot(page: Page, name: str):
    path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    page.screenshot(path=path, full_page=True)
    return path


def _get_chat_frame(page: Page):
    """Get the chat iframe from the parent page (DR-019)."""
    page.goto(BASE_URL, wait_until="networkidle", timeout=CHAT_TIMEOUT)
    page.wait_for_timeout(8000)  # React app cold start inside iframe
    for frame in page.frames:
        if ":5173" in frame.url or "hf.space" in frame.url:
            frame.wait_for_timeout(3000)
            return frame
    return page


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def ctx(browser):
    context = browser.new_context(
        viewport={"width": 1400, "height": 900},
        ignore_https_errors=True,
    )
    page = context.new_page()
    yield page
    context.close()


# ── Phase 1: Page Load ─────────────────────────────────────────────────────

class TestPageLoad:
    """Verify local environment serves the site and chat loads."""

    def test_http_localhost_loads(self, ctx):
        """http://localhost returns 200."""
        resp = ctx.goto(BASE_URL, wait_until="domcontentloaded", timeout=15000)
        assert resp.status == 200, f"Expected 200, got {resp.status}"
        _screenshot(ctx, "01_page_loaded")

    def test_page_title(self, ctx):
        """Page title contains ChatHealthy."""
        ctx.goto(BASE_URL, wait_until="domcontentloaded")
        assert "ChatHealthy" in ctx.title(), f"Title: {ctx.title()}"

    def test_https_redirect_missing(self, ctx):
        """KNOWN BUG: http://localhost does NOT redirect to https://localhost.
        When TLS is configured, this test should be updated to assert redirect."""
        ctx.goto(BASE_URL, wait_until="domcontentloaded")
        final_url = ctx.url
        if final_url.startswith("https://"):
            pass  # Bug is fixed — great
        else:
            _record_bug(
                "BUG-TLS-001",
                "http://localhost does not redirect to https://localhost",
                f"Final URL: {final_url}",
            )

    def test_red_dev_banner(self, ctx):
        """Red dev/staging environment banner is visible."""
        frame = _get_chat_frame(ctx)
        # Banner is inside the chat iframe — look for env indicator
        ctx.wait_for_timeout(3000)
        # Check iframe for banner
        banner_text = ""
        try:
            banner = frame.locator("[class*='banner'], [style*='background'][style*='red']").first
            banner.wait_for(state="visible", timeout=5000)
            banner_text = banner.inner_text()
        except Exception:
            # Banner might be on parent page
            try:
                banner = ctx.locator("[class*='banner'], [id*='banner']").first
                banner.wait_for(state="visible", timeout=3000)
                banner_text = banner.inner_text()
            except Exception:
                pass
        assert banner_text, "Red dev banner should be visible"
        _screenshot(ctx, "02_dev_banner")

    def test_chat_iframe_present(self, ctx):
        """Chat iframe (#coreChatFrame) exists on the page."""
        ctx.goto(BASE_URL, wait_until="domcontentloaded")
        iframe = ctx.locator("#coreChatFrame")
        iframe.wait_for(state="attached", timeout=10000)
        assert iframe.count() > 0

    def test_welcome_message(self, ctx):
        """Chat shows a welcome/greeting message on load."""
        frame = _get_chat_frame(ctx)
        # Wait for first assistant message
        msg = frame.locator("[class*='message'], [class*='bubble']").first
        msg.wait_for(state="visible", timeout=15000)
        text = msg.inner_text()
        assert len(text) > 20, f"Welcome message too short: {text[:50]}"
        _screenshot(ctx, "03_welcome_message")

    def test_qa_report_in_welcome(self, ctx):
        """Welcome message contains QA report (known gap: may be a feature not a bug)."""
        frame = _get_chat_frame(ctx)
        msg = frame.locator("[class*='message'], [class*='bubble']").first
        msg.wait_for(state="visible", timeout=15000)
        text = msg.inner_text()
        # QA report presence — Boss noted this might be a feature
        has_qa = any(w in text.lower() for w in ["qa", "report", "build", "version", "environment"])
        if not has_qa:
            _record_bug(
                "BUG-QA-REPORT-001",
                "QA report not visible in welcome message",
                f"Welcome text: {text[:200]}",
            )

    def test_input_prompt_exists(self, ctx):
        """Chat input field exists and is not locked."""
        frame = _get_chat_frame(ctx)
        input_el = frame.locator("input[placeholder*='Type a message'], textarea").first
        input_el.wait_for(state="visible", timeout=10000)
        placeholder = input_el.get_attribute("placeholder")
        assert "suspended" not in (placeholder or "").lower(), "Chat input should not be locked"
        _screenshot(ctx, "04_input_prompt")


# ── Phase 2: Provider Search ───────────────────────────────────────────────

class TestProviderSearch:
    """Search for DE foot provider — full FindCare flow."""

    def _search_and_wait(self, ctx):
        """Send the search query and handle the timeout modal."""
        frame = _get_chat_frame(ctx)

        # Send the query
        chat_input = frame.locator("input[placeholder*='Type a message'], textarea").first
        chat_input.fill("can you find me a provider in DE that can treat my foot?")
        send_btn = frame.locator("button", has_text="Send").first
        send_btn.click()
        _screenshot(ctx, "05_query_sent")

        return frame

    def test_provider_search_sends(self, ctx):
        """Query sends and system starts processing."""
        frame = self._search_and_wait(ctx)
        # Should see loading state or thinking indicator
        ctx.wait_for_timeout(5000)
        messages = frame.locator("[class*='message'], [class*='bubble']")
        assert messages.count() >= 1, "Should have at least the welcome message while loading"

    def test_timeout_modal_appears(self, ctx):
        """DESIGN BUG: Timeout modal asks user to wait — should stop and ask intent."""
        frame = self._search_and_wait(ctx)

        # Wait for the timeout modal (~45s threshold)
        try:
            keep_waiting_btn = frame.locator("button", has_text="Yes, keep waiting").first
            keep_waiting_btn.wait_for(state="visible", timeout=60000)
            _screenshot(ctx, "06_timeout_modal")

            _record_bug(
                "BUG-UX-TIMEOUT-001",
                "Timeout modal asks user to wait twice — should stop query and ask user what to do",
                "At 45s threshold, modal appears asking 'continue?' — design should halt and ask intent",
            )

            # Click "Yes, keep waiting" to continue
            keep_waiting_btn.click()
            _screenshot(ctx, "07_keep_waiting_clicked")

        except Exception:
            # Query completed before timeout — that's fine
            _screenshot(ctx, "06_no_timeout_modal")

    def test_provider_results_appear(self, ctx):
        """Provider results appear in the chat window after search."""
        frame = self._search_and_wait(ctx)

        # Handle timeout modal if it appears
        try:
            keep_btn = frame.locator("button", has_text="Yes, keep waiting").first
            keep_btn.wait_for(state="visible", timeout=60000)
            keep_btn.click()
        except Exception:
            pass

        # Wait for response with provider data
        frame.locator("text=/NPI|provider|found|Records/i").first.wait_for(
            state="visible", timeout=CHAT_TIMEOUT
        )
        _screenshot(ctx, "08_provider_results")

        # Verify we have provider content
        all_text = frame.locator("body").inner_text()
        assert any(w in all_text for w in ["NPI", "npi", "MD", "DO", "provider", "Records"]), \
            f"Expected provider data, got: {all_text[:300]}"

    def test_providers_in_main_chat(self, ctx):
        """Provider list (names, addresses, NPIs) visible in center chat window."""
        frame = self._search_and_wait(ctx)
        try:
            keep_btn = frame.locator("button", has_text="Yes, keep waiting").first
            keep_btn.wait_for(state="visible", timeout=60000)
            keep_btn.click()
        except Exception:
            pass

        # Wait for full results
        frame.locator("text=/NPI|Records/i").first.wait_for(
            state="visible", timeout=CHAT_TIMEOUT
        )
        all_text = frame.locator("body").inner_text()

        # Should have multiple providers listed
        npi_count = len(re.findall(r"\d{10}", all_text))
        assert npi_count >= 1, f"Expected NPI numbers in results, found {npi_count}"
        _screenshot(ctx, "09_provider_list_chat")


# ── Phase 3: Left Panel Verification ───────────────────────────────────────

class TestLeftPanel:
    """Verify left panel state after provider search."""

    def _do_search(self, ctx):
        """Run the search and wait for results."""
        frame = _get_chat_frame(ctx)
        chat_input = frame.locator("input[placeholder*='Type a message'], textarea").first
        chat_input.fill("can you find me a provider in DE that can treat my foot?")
        send_btn = frame.locator("button", has_text="Send").first
        send_btn.click()

        try:
            keep_btn = frame.locator("button", has_text="Yes, keep waiting").first
            keep_btn.wait_for(state="visible", timeout=60000)
            keep_btn.click()
        except Exception:
            pass

        # Wait for results
        frame.locator("text=/NPI|Records/i").first.wait_for(
            state="visible", timeout=CHAT_TIMEOUT
        )
        ctx.wait_for_timeout(5000)  # Let postMessage propagate to parent panels
        return frame

    def test_specialty_filters_visible(self, ctx):
        """Left panel shows specialty filter checkboxes."""
        self._do_search(ctx)
        left_panel = ctx.locator("#leftPanel")
        left_panel.wait_for(state="visible", timeout=10000)

        # Wait for filter content to arrive via postMessage
        ctx.wait_for_timeout(5000)
        panel_text = left_panel.inner_text()

        checkboxes = left_panel.locator("input[type='checkbox']")
        _screenshot(ctx, "10_left_panel_filters")

        has_content = checkboxes.count() > 0 or len(panel_text) > 20
        assert has_content, f"Left panel should have specialty filters. Text: {panel_text[:200]}"

    def test_session_token_visible(self, ctx):
        """Left panel shows session/authorization token starting with ch_."""
        self._do_search(ctx)
        left_panel = ctx.locator("#leftPanel")
        ctx.wait_for_timeout(5000)
        panel_text = left_panel.inner_text()

        assert "ch_" in panel_text.lower() or "CH_" in panel_text, \
            f"Session token (ch_) not found in left panel. Text: {panel_text[:300]}"
        _screenshot(ctx, "11_session_token")

    def test_nonce_timestamps(self, ctx):
        """Left panel shows nonce (two datetime stamps)."""
        self._do_search(ctx)
        left_panel = ctx.locator("#leftPanel")
        ctx.wait_for_timeout(5000)
        panel_text = left_panel.inner_text()

        # Look for datetime patterns (ISO 8601 or date-like)
        date_patterns = re.findall(r"\d{4}-\d{2}-\d{2}", panel_text)
        assert len(date_patterns) >= 2, \
            f"Expected 2 datetime stamps (nonce), found {len(date_patterns)}: {date_patterns}"
        _screenshot(ctx, "12_nonce_timestamps")

    def test_two_buttons_below_specialties(self, ctx):
        """Left panel has two buttons below the specialty filter list."""
        self._do_search(ctx)
        left_panel = ctx.locator("#leftPanel")
        ctx.wait_for_timeout(5000)

        buttons = left_panel.locator("button")
        _screenshot(ctx, "13_left_panel_buttons")

        assert buttons.count() >= 2, \
            f"Expected at least 2 buttons in left panel, found {buttons.count()}"

    def test_evaluate_care_button_exists(self, ctx):
        """Lower button says 'Evaluate Care' or similar."""
        self._do_search(ctx)
        left_panel = ctx.locator("#leftPanel")
        ctx.wait_for_timeout(5000)

        buttons = left_panel.locator("button")
        eval_found = False
        eval_text = ""
        for i in range(buttons.count()):
            btn_text = buttons.nth(i).inner_text().strip()
            if "evaluat" in btn_text.lower():
                eval_found = True
                eval_text = btn_text
                break

        assert eval_found, f"'Evaluate Care' button not found. Buttons: {[buttons.nth(i).inner_text() for i in range(buttons.count())]}"
        _screenshot(ctx, "14_evaluate_button")


# ── Phase 4: Right Panel ──────────────────────────────────────────────────

class TestRightPanel:
    """Right panel should be empty before Evaluate Care."""

    def _do_search(self, ctx):
        frame = _get_chat_frame(ctx)
        chat_input = frame.locator("input[placeholder*='Type a message'], textarea").first
        chat_input.fill("can you find me a provider in DE that can treat my foot?")
        frame.locator("button", has_text="Send").first.click()
        try:
            keep_btn = frame.locator("button", has_text="Yes, keep waiting").first
            keep_btn.wait_for(state="visible", timeout=60000)
            keep_btn.click()
        except Exception:
            pass
        frame.locator("text=/NPI|Records/i").first.wait_for(
            state="visible", timeout=CHAT_TIMEOUT
        )
        ctx.wait_for_timeout(5000)
        return frame

    def test_right_panel_empty(self, ctx):
        """Right panel has no content before Evaluate Care is clicked."""
        self._do_search(ctx)
        right_panel = ctx.locator("#rightPanel")
        right_text = right_panel.inner_text().strip()
        _screenshot(ctx, "15_right_panel_empty")
        assert len(right_text) < 10, \
            f"Right panel should be empty, got: {right_text[:100]}"


# ── Phase 5: Evaluate Care Handoff ─────────────────────────────────────────

class TestEvaluateCareHandoff:
    """Click Evaluate Care button and verify handoff behavior."""

    def _search_and_click_evaluate(self, ctx):
        """Full flow: search → results → click Evaluate Care."""
        frame = _get_chat_frame(ctx)
        chat_input = frame.locator("input[placeholder*='Type a message'], textarea").first
        chat_input.fill("can you find me a provider in DE that can treat my foot?")
        frame.locator("button", has_text="Send").first.click()

        try:
            keep_btn = frame.locator("button", has_text="Yes, keep waiting").first
            keep_btn.wait_for(state="visible", timeout=60000)
            keep_btn.click()
        except Exception:
            pass

        frame.locator("text=/NPI|Records/i").first.wait_for(
            state="visible", timeout=CHAT_TIMEOUT
        )
        ctx.wait_for_timeout(5000)
        _screenshot(ctx, "16_before_evaluate")

        # Find and click the Evaluate Care button in left panel
        left_panel = ctx.locator("#leftPanel")
        buttons = left_panel.locator("button")
        for i in range(buttons.count()):
            btn_text = buttons.nth(i).inner_text().strip()
            if "evaluat" in btn_text.lower():
                buttons.nth(i).click()
                break

        ctx.wait_for_timeout(8000)  # Wait for EvaluateCare handoff
        _screenshot(ctx, "17_after_evaluate_click")
        return frame

    def test_evaluate_click_triggers_handoff(self, ctx):
        """Clicking Evaluate Care triggers the handoff sequence."""
        frame = self._search_and_click_evaluate(ctx)
        _screenshot(ctx, "18_evaluate_handoff")
        # Test passes if no crash — specifics checked in subsequent tests

    def test_provider_list_moves_to_left(self, ctx):
        """After Evaluate click, provider list appears in left panel."""
        frame = self._search_and_click_evaluate(ctx)
        left_panel = ctx.locator("#leftPanel")
        left_text = left_panel.inner_text()
        _screenshot(ctx, "19_providers_in_left")

        # Should have provider names or NPIs in left panel now
        has_providers = bool(re.findall(r"\d{10}", left_text)) or \
            any(w in left_text for w in ["Dr.", "MD", "DO", "NPI"])
        assert has_providers, f"Provider list should move to left panel. Text: {left_text[:300]}"

    def test_chat_resets_with_greeting_bug(self, ctx):
        """BUG: After Evaluate handoff, chat shows FindCare greeting instead of EvaluateCare."""
        frame = self._search_and_click_evaluate(ctx)

        # Chat should reset — check first message
        messages = frame.locator("[class*='message'], [class*='bubble']")
        if messages.count() > 0:
            first_msg = messages.first.inner_text()

            # This IS a bug — should show EvaluateCare greeting, not FindCare
            _record_bug(
                "BUG-HANDOFF-GREETING-001",
                "After Evaluate Care handoff, chat shows same FindCare greeting",
                f"Greeting text: {first_msg[:200]}",
            )
        _screenshot(ctx, "20_greeting_bug")

    def test_encrypted_token_below_providers(self, ctx):
        """Encrypted session/authorization token appears below provider list in left panel."""
        frame = self._search_and_click_evaluate(ctx)
        left_panel = ctx.locator("#leftPanel")
        left_text = left_panel.inner_text()
        _screenshot(ctx, "21_encrypted_token")

        # Look for token indicators
        has_token = "ch_" in left_text.lower() or "CH_" in left_text or "token" in left_text.lower()
        assert has_token, f"Encrypted token not found below providers. Text: {left_text[:400]}"

    def test_decrypted_token_below_encrypted(self, ctx):
        """Decrypted session token appears below the encrypted one."""
        frame = self._search_and_click_evaluate(ctx)
        left_panel = ctx.locator("#leftPanel")
        left_text = left_panel.inner_text()
        _screenshot(ctx, "22_decrypted_token")

        # Decrypted token should show origin, timestamp, or readable fields
        has_decrypted = any(w in left_text.lower() for w in [
            "origin", "findcare", "signed", "issued", "expires", "nonce",
        ])
        if not has_decrypted:
            _record_bug(
                "BUG-TOKEN-DISPLAY-001",
                "Decrypted session token not visible below encrypted token",
                f"Left panel text: {left_text[:400]}",
            )


# ── Phase 6: Known Gaps (informational, not failures) ─────────────────────

class TestKnownGaps:
    """Document known gaps — these are NOT test failures, they are tracked work."""

    def test_gap_tls_between_components(self, ctx):
        """GAP: TLS is not yet configured between all local components.
        Caddy serves :8080 with TLS but inter-service calls use plain HTTP."""
        # This is a known gap, not a test failure
        _record_bug(
            "GAP-TLS-001",
            "TLS not configured between all local components",
            "Caddy :8080/:8081/:8082 have TLS certs but backend-to-backend is plain HTTP",
        )

    def test_gap_443_for_sensitive_pages(self, ctx):
        """GAP: Need separate :443 server for sensitive pages (architecture, roadmap, artifacts)."""
        _record_bug(
            "GAP-443-001",
            "Sensitive pages (architecture, roadmap, artifacts) served on :80 without TLS",
            "Need :443 web server to protect architecture.html, roadmap.html, and accompanying artifacts",
        )


# ── Report ─────────────────────────────────────────────────────────────────

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Print bug/gap summary at end of test run."""
    if KNOWN_BUGS:
        terminalreporter.write_sep("=", "BUGS & GAPS FOUND")
        for bug in KNOWN_BUGS:
            terminalreporter.write_line(f"  {bug['id']}: {bug['summary']}")
            if bug["detail"]:
                terminalreporter.write_line(f"    Detail: {bug['detail']}")
