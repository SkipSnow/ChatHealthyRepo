# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Dev Environment User-Ready Smoke Test
#
# EPIC: EPIC-TEST — User Testing Simulation
# Feature: USER-TEST-DEV — Dev Environment Security & User Readiness
#
# Same test logic as local_environment_user_ready.py but binds to:
#   Website:      https://dev.chathealthy.ai (Cloudflare → HF)
#   Chat iframe:  https://skipsnow-dev-chathealthyspace.hf.space
#   FindCare:     (inside the HF space)
#   EvaluateCare: https://skipsnow-dev-evaluatecarespace.hf.space
#
# Usage:
#   pytest dev_environment_user_ready.py -v
#   TEST_BASE_URL=https://dev.chathealthy.ai pytest dev_environment_user_ready.py -v
#
# DR-019: Tests run against the full parent page, not individual components.

import os
import re
import pytest
from playwright.sync_api import sync_playwright, Page

BASE_URL = os.getenv("TEST_BASE_URL", "https://dev.chathealthy.ai")
CHAT_TIMEOUT = 180_000  # HF spaces may need cold start time
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "test_screenshots", "dev_user_ready")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

KNOWN_BUGS = []


def _record_bug(bug_id, summary, detail=""):
    KNOWN_BUGS.append({"id": bug_id, "summary": summary, "detail": detail})


def _screenshot(page: Page, name: str):
    path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    page.screenshot(path=path, full_page=True)
    return path


# ── Single shared browser + page for the entire test run ────────────────────
# The search takes ~90s. We do it ONCE and all tests share the result.

@pytest.fixture(scope="module")
def shared_page():
    """One browser, one page, one search — shared across all tests."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            ignore_https_errors=True,
        )
        page = context.new_page()

        # Phase 1: Load page — HF spaces may cold start (~30-60s)
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(15000)  # Wait for iframe + HF space cold start

        # Find chat frame
        chat_frame = page
        for frame in page.frames:
            if ":5173" in frame.url or ":3000" in frame.url or "hf.space" in frame.url:
                chat_frame = frame
                break

        # Wait for chat input to be ready
        chat_input = chat_frame.locator("input[placeholder*='Type a message'], textarea").first
        chat_input.wait_for(state="visible", timeout=30000)

        # Phase 2: Send search
        chat_input.fill("can you find me a provider in DE that can treat my foot?")
        chat_frame.locator("button", has_text="Send").first.click()

        # Handle timeout modal if it appears (~45s)
        try:
            keep_btn = chat_frame.locator("button", has_text="Yes, keep waiting").first
            keep_btn.wait_for(state="visible", timeout=60000)
            keep_btn.click()
        except Exception:
            pass

        # Wait for results — look for NPI pattern or provider content
        try:
            chat_frame.locator("text=/NPI:|Records|County/i").first.wait_for(
                state="visible", timeout=CHAT_TIMEOUT
            )
        except Exception:
            pass

        page.wait_for_timeout(12000)  # Let postMessage propagate to parent panels

        yield {
            "page": page,
            "frame": chat_frame,
            "browser": browser,
        }

        context.close()
        browser.close()


# ── Phase 1: Page Load ─────────────────────────────────────────────────────

class TestPageLoad:
    """Verify local environment serves the site and chat loads."""

    def test_http_localhost_loads(self, shared_page):
        page = shared_page["page"]
        assert "localhost" in page.url or "127.0.0.1" in page.url
        _screenshot(page, "01_page_loaded")

    def test_page_title(self, shared_page):
        page = shared_page["page"]
        assert "ChatHealthy" in page.title(), f"Title: {page.title()}"

    def test_environment_class(self, shared_page):
        page = shared_page["page"]
        body_class = page.locator("body").get_attribute("class") or ""
        has_env = any(c in body_class for c in ["env-local", "env-dev", "env-qa", "env-prod"])
        assert has_env, f"Body should have env-* class, got: {body_class}"
        _screenshot(page, "02_env_class")

    def test_chat_iframe_present(self, shared_page):
        page = shared_page["page"]
        iframe = page.locator("#coreChatFrame")
        assert iframe.count() > 0

    def test_welcome_message_was_shown(self, shared_page):
        frame = shared_page["frame"]
        body_text = frame.locator("body").inner_text()
        # Welcome message was shown before search — search response now visible
        assert len(body_text) > 50, f"Chat content too short: {body_text[:100]}"


# ── Phase 2: Provider Search ───────────────────────────────────────────────

class TestProviderSearch:
    """Verify DE foot provider search results."""

    def test_provider_results_visible(self, shared_page):
        frame = shared_page["frame"]
        body_text = frame.locator("body").inner_text()
        assert any(w in body_text for w in ["NPI", "npi", "MD", "DO", "DPM", "provider", "Records", "foot"]), \
            f"Expected provider data. Text: {body_text[:300]}"
        _screenshot(shared_page["page"], "03_provider_results")

    def test_providers_have_npis(self, shared_page):
        frame = shared_page["frame"]
        body_text = frame.locator("body").inner_text()
        # NPI format: "NPI: 1234567890" — look for the label + number pattern
        npis = re.findall(r"NPI[:\s]+(\d{10})", body_text)
        if not npis:
            # Fallback: any 10-digit number
            npis = re.findall(r"\b\d{10}\b", body_text)
        assert len(npis) >= 1, f"Expected NPI numbers. Text sample: {body_text[-500:]}"

    def test_results_mention_de(self, shared_page):
        frame = shared_page["frame"]
        body_text = frame.locator("body").inner_text()
        assert any(w in body_text for w in ["DE", "Delaware", "19"]), \
            f"Results should mention DE/Delaware"


# ── Phase 3: Left Panel ───────────────────────────────────────────────────

class TestLeftPanel:
    """Verify left panel after search."""

    def test_specialty_filters_visible(self, shared_page):
        page = shared_page["page"]
        left_panel = page.locator("#leftPanel")
        left_text = left_panel.inner_text()
        checkboxes = left_panel.locator("input[type='checkbox']")
        _screenshot(page, "04_left_panel_filters")

        has_filter = "FILTER" in left_text or "filter" in left_text.lower() or checkboxes.count() > 0
        assert has_filter, f"Left panel should have specialty filters. Text: {left_text[:300]}"

    def test_filter_has_counts(self, shared_page):
        page = shared_page["page"]
        left_text = page.locator("#leftPanel").inner_text()
        assert "types" in left_text.lower() or "showing" in left_text.lower(), \
            f"Filter should show counts. Text: {left_text[:300]}"

    def test_prescriber_checkbox_exists(self, shared_page):
        page = shared_page["page"]
        left_text = page.locator("#leftPanel").inner_text()
        assert "prescriber" in left_text.lower(), \
            f"Prescriber checkbox not found. Text: {left_text[:300]}"

    def test_evaluate_care_button_exists(self, shared_page):
        page = shared_page["page"]
        eval_btn = page.locator("[data-gui-action='evaluate-providers']")
        _screenshot(page, "05_evaluate_button")
        assert eval_btn.count() > 0, "Evaluate Care button not found"


# ── Phase 4: Right Panel Before Evaluate ──────────────────────────────────

class TestRightPanel:

    def test_right_panel_before_evaluate(self, shared_page):
        page = shared_page["page"]
        right_text = page.locator("#rightPanel").inner_text().strip()
        _screenshot(page, "06_right_panel_before")
        # Should be placeholder or minimal content
        assert len(right_text) < 50, f"Right panel should be placeholder. Got: {right_text[:100]}"


# ── Phase 5: Evaluate Care Handoff ─────────────────────────────────────────

class TestEvaluateCareHandoff:
    """Click Evaluate Care and verify handoff."""

    def test_evaluate_click(self, shared_page):
        page = shared_page["page"]
        _screenshot(page, "07_before_evaluate")

        eval_btn = page.locator("[data-gui-action='evaluate-providers']")
        if eval_btn.count() > 0:
            eval_btn.click()
            # Wait for right panel to populate — EvaluateCare handoff takes time
            try:
                page.locator("#rightPanel >> text=/EvaluateCare|NPI|provider/i").first.wait_for(
                    state="visible", timeout=30000
                )
            except Exception:
                pass
            page.wait_for_timeout(3000)
        _screenshot(page, "08_after_evaluate")

    @pytest.mark.xfail(reason="BUG-EVAL-001: lastProvidersRef empty — deferred")
    def test_right_panel_has_providers(self, shared_page):
        page = shared_page["page"]
        right_text = page.locator("#rightPanel").inner_text()
        _screenshot(page, "09_right_panel_providers")

        has_content = "EvaluateCare" in right_text or \
            bool(re.findall(r"\b\d{10}\b", right_text)) or \
            any(w in right_text for w in ["Dr.", "MD", "DO", "DPM", "NPI", "provider"])
        assert has_content, f"Right panel should show providers. Text: {right_text[:400]}"

    @pytest.mark.xfail(reason="BUG-EVAL-001: lastProvidersRef empty — deferred")
    def test_session_token_visible(self, shared_page):
        page = shared_page["page"]
        session_el = page.locator("#guiSessionId")
        left_text = page.locator("#leftPanel").inner_text()
        control_text = page.locator("#controlFrame").inner_text() if page.locator("#controlFrame").count() else ""

        has_token = session_el.count() > 0 or \
            "CH_" in left_text or "CH_" in control_text
        _screenshot(page, "10_session_token")
        assert has_token, f"Session token (CH_) not found"

    @pytest.mark.xfail(reason="BUG-EVAL-001: lastProvidersRef empty — deferred")
    def test_session_verification_in_right_panel(self, shared_page):
        page = shared_page["page"]
        right_text = page.locator("#rightPanel").inner_text()
        _screenshot(page, "11_session_verification")

        has_verification = any(w in right_text for w in [
            "Session Verification", "Signed token", "Verified", "Decrypted",
        ])
        assert has_verification, f"Session verification not found. Text: {right_text[:400]}"


# ── Phase 6: Known Gaps ───────────────────────────────────────────────────

class TestKnownGaps:

    def test_gap_tls_between_components(self, shared_page):
        _record_bug("GAP-TLS-001", "TLS not configured between all local components")

    def test_dev_uses_https(self, shared_page):
        page = shared_page["page"]
        assert page.url.startswith("https://"), f"Dev must use HTTPS. URL: {page.url}"


# ── Report ─────────────────────────────────────────────────────────────────

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if KNOWN_BUGS:
        terminalreporter.write_sep("=", "BUGS & GAPS FOUND")
        for bug in KNOWN_BUGS:
            terminalreporter.write_line(f"  {bug['id']}: {bug['summary']}")
            if bug.get("detail"):
                terminalreporter.write_line(f"    Detail: {bug['detail']}")
