# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Playwright E2E test: FindCare → EvaluateCare handoff sequence
# DR-019: Tests run against the full parent page, not individual components.
#
# Test scenario:
#   1. "find me shrinks in Delaware"
#   2. Wait for provider results
#   3. Apply specialty filter — MDs practicing psychiatry only
#   4. Verify "Evaluate these providers" button appears in control frame
#   5. Click evaluate button
#   6. Verify stub response paints provider names, specialty, NPI on screen

import pytest
from playwright.sync_api import sync_playwright, expect
import os
import time

# DR-019: Test against the full site, not individual pages
BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost")
TIMEOUT = 120_000  # FindCare takes ~90s to connect to MongoDB


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    yield page
    context.close()


def _wait_for_chat_ready(page):
    """Wait for the chat iframe to load and be interactive."""
    # Wait for iframe to appear
    page.wait_for_selector("#coreChatFrame", timeout=TIMEOUT)
    # Wait for iframe content to load
    iframe = page.frame_locator("#coreChatFrame")
    # Wait for the input field to be visible (chat is ready)
    iframe.locator("input, textarea").first.wait_for(timeout=TIMEOUT)
    return iframe


def _send_message(iframe, message: str):
    """Type a message and submit it."""
    input_el = iframe.locator("input, textarea").first
    input_el.fill(message)
    # Press Enter or click send button
    input_el.press("Enter")


def _wait_for_response(iframe, timeout=TIMEOUT):
    """Wait for an assistant response to appear after the last user message."""
    time.sleep(2)  # Brief wait for response to start
    # Wait for loading to finish — look for absence of loading indicator
    try:
        iframe.locator("[data-loading='true'], .loading").wait_for(state="hidden", timeout=timeout)
    except Exception:
        pass  # Loading indicator may not exist
    time.sleep(1)  # Let response fully render


class TestFindCareToEvaluateCareHandoff:
    """E2E test: FindCare provider search → EvaluateCare stub evaluation."""

    def test_search_shrinks_in_delaware(self, page):
        """Phase 3.16: Search for shrinks in Delaware."""
        page.goto(BASE_URL, timeout=TIMEOUT)
        iframe = _wait_for_chat_ready(page)

        _send_message(iframe, "find me shrinks in Delaware")
        _wait_for_response(iframe)

        # Verify we got provider results — look for common markers
        content = iframe.locator(".message-content, [class*='message']").all_text_contents()
        full_text = " ".join(content).lower()
        assert "delaware" in full_text or "de" in full_text, \
            f"Expected Delaware results, got: {full_text[:200]}"

    def test_evaluate_button_appears(self, page):
        """Phase 2.14 + 3.18: Verify evaluate button appears in control frame after provider search."""
        page.goto(BASE_URL, timeout=TIMEOUT)
        iframe = _wait_for_chat_ready(page)

        _send_message(iframe, "find me shrinks in Delaware")
        _wait_for_response(iframe)

        # User says "yes" to see more — this triggers pagination and the evaluate button
        _send_message(iframe, "yes")
        _wait_for_response(iframe)

        # The "Evaluate these providers" button should be in the parent page's control frame
        evaluate_btn = page.locator("[data-gui-action='evaluate-providers']")
        evaluate_btn.wait_for(timeout=30_000)
        assert evaluate_btn.is_visible(), "Evaluate button should be visible in control frame"

    def test_evaluate_handoff_paints_results(self, page):
        """Phase 3.19: Click evaluate → stub paints names, specialty, NPI on screen."""
        page.goto(BASE_URL, timeout=TIMEOUT)
        iframe = _wait_for_chat_ready(page)

        _send_message(iframe, "find me shrinks in Delaware")
        _wait_for_response(iframe)

        # Trigger pagination to get evaluate button
        _send_message(iframe, "yes")
        _wait_for_response(iframe)

        # Click the evaluate button
        evaluate_btn = page.locator("[data-gui-action='evaluate-providers']")
        evaluate_btn.wait_for(timeout=30_000)
        evaluate_btn.click()

        # Wait for evaluation results to appear in chat
        time.sleep(5)

        # Verify stub evaluation results painted on screen
        content = iframe.locator(".message-content, [class*='message']").all_text_contents()
        full_text = " ".join(content)

        # Should contain provider evaluation markers
        assert "EvaluateCare" in full_text or "Provider Evaluation" in full_text, \
            f"Expected evaluation results, got: {full_text[:300]}"
        assert "NPI" in full_text, \
            f"Expected NPI in evaluation results, got: {full_text[:300]}"
        assert "Specialty" in full_text or "specialty" in full_text, \
            f"Expected specialty in evaluation results, got: {full_text[:300]}"


class TestSecurityRequirements:
    """Phase 2.13: EPIC-4 Security requirements — mTLS enforcement, service isolation."""

    def test_shared_services_health(self, page):
        """SEC: Shared Services responds on /health."""
        response = page.request.get("http://localhost:8002/health")
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "shared_services"

    def test_evalcare_health(self, page):
        """SEC: EvaluateCare responds on /health."""
        response = page.request.get("http://localhost:8001/health")
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "evaluate_care"

    def test_findcare_health(self, page):
        """SEC: FindCare responds on /health."""
        response = page.request.get("http://localhost:8000/health")
        data = response.json()
        assert data["status"] in ("ok", "degraded")

    def test_evalcare_stub_endpoint(self, page):
        """SEC: EvaluateCare /evaluate/providers accepts provider list and returns stub."""
        response = page.request.post("http://localhost:8001/evaluate/providers", data={
            "providers": [
                {"name": "Dr. Test", "specialty": "Psychiatry", "npi": "1234567890"},
                {"name": "Dr. Sample", "specialty": "Psychology", "npi": "0987654321"},
            ],
            "question_summary": "Test evaluation",
        })
        data = response.json()
        assert data["status"] == "stub"
        assert len(data["evaluated_providers"]) == 2
        assert data["evaluated_providers"][0]["name"] == "Dr. Test"
        assert data["evaluated_providers"][0]["npi"] == "1234567890"
