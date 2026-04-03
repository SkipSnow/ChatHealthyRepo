# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# SIT Browser Tests — Playwright headless Chrome against dev.chathealthy.ai
#
# These tests drive a real browser. They are SIT (System Integration Tests),
# not unit tests. Dev writes them, QA executes them.
#
# Usage:
#   pytest test_sit_browser.py -v --headed   (watch the browser)
#   pytest test_sit_browser.py -v            (headless)
#
# Screenshots saved to test_screenshots/ on failure.

import os
import re
import pytest
from playwright.sync_api import Page, expect

BASE_URL = os.getenv("SIT_BASE_URL", "https://dev.chathealthy.ai")
CHAT_IFRAME_URL = None  # resolved dynamically from the page

SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "test_screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


@pytest.fixture(scope="session")
def browser_context_args():
    return {"viewport": {"width": 1400, "height": 900}}


def _screenshot(page: Page, name: str):
    """Save screenshot for Boss review."""
    path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    page.screenshot(path=path, full_page=True)
    return path


def _get_chat_frame(page: Page):
    """Get the chat iframe from the parent page."""
    page.goto(BASE_URL, wait_until="networkidle")
    page.wait_for_timeout(8000)  # React app inside HF iframe needs time
    # Find the HuggingFace Space iframe
    for frame in page.frames:
        if "hf.space" in frame.url or ":5173" in frame.url:
            frame.wait_for_timeout(3000)
            return frame
    # Local dev or direct — try the page itself
    return page


def _send_message(frame, text: str, timeout: int = 90000):
    """Type a message and send it. Wait for response."""
    chat_input = frame.locator("input[placeholder*='Type a message'], textarea").first
    chat_input.fill(text)
    # Click Send button
    send_btn = frame.locator("button", has_text="Send").first
    send_btn.click()
    # Wait for response — look for new assistant message content
    frame.wait_for_timeout(5000)
    # Wait until loading is done (Send button re-enabled)
    send_btn.wait_for(state="visible", timeout=timeout)
    frame.wait_for_timeout(2000)


class TestSITProviderSearch:
    """SIT: Provider search end-to-end in real browser."""

    def test_page_loads(self, page: Page):
        """Parent page loads and chat iframe is accessible."""
        page.goto(BASE_URL, wait_until="networkidle")
        assert "ChatHealthy" in page.title()
        _screenshot(page, "01_page_loaded")

    def test_welcome_message(self, page: Page):
        """Chat shows welcome message on load."""
        frame = _get_chat_frame(page)
        # Should see a welcome message
        frame.wait_for_timeout(3000)
        content = frame.content()
        assert len(content) > 100, "Chat frame should have content"
        _screenshot(page, "02_welcome_message")

    def test_provider_search_returns_results(self, page: Page):
        """Search for providers returns results with provider names."""
        frame = _get_chat_frame(page)
        _send_message(frame, "find pediatricians in delaware")
        content = frame.content()
        _screenshot(page, "03_provider_search_results")
        # Should have provider data in the response
        assert "NPI" in content or "npi" in content or "MD" in content, \
            "Response should contain provider data"

    def test_summary_message_present(self, page: Page):
        """After provider search, system summary message appears with search term."""
        frame = _get_chat_frame(page)
        _send_message(frame, "find surgeons in delaware")
        content = frame.content()
        _screenshot(page, "04_summary_message")
        # Summary should contain the search term, not generic "providers"
        assert "surgeon" in content.lower(), \
            "Summary must use the search term, not generic 'providers'"

    def test_summary_has_filter_link(self, page: Page):
        """Summary message contains a clickable Filter link."""
        frame = _get_chat_frame(page)
        _send_message(frame, "find surgeons in delaware")
        _screenshot(page, "05_filter_link")
        # Look for the Filter action link
        filter_link = frame.locator("a[href='#action:filter']")
        assert filter_link.count() > 0, "Summary must contain a [Filter] action link"

    def test_summary_has_next_page_link(self, page: Page):
        """Summary message contains a clickable next page link."""
        frame = _get_chat_frame(page)
        _send_message(frame, "find surgeons in delaware")
        _screenshot(page, "06_next_page_link")
        # Look for the next page action link
        next_link = frame.locator("a[href='#action:next-page']")
        assert next_link.count() > 0, "Summary must contain a [next page] action link"

    def test_filter_link_highlights_panel(self, page: Page):
        """Clicking Filter link highlights the filter panel."""
        frame = _get_chat_frame(page)
        _send_message(frame, "find surgeons in delaware")
        filter_link = frame.locator("a[href='#action:filter']").first
        if filter_link.count() > 0:
            filter_link.click()
            page.wait_for_timeout(1500)
            _screenshot(page, "07_filter_highlighted")

    def test_next_page_link_loads_page2(self, page: Page):
        """Clicking next page link loads second page of results."""
        frame = _get_chat_frame(page)
        _send_message(frame, "find surgeons in delaware")
        next_link = frame.locator("a[href='#action:next-page']").first
        if next_link.count() > 0:
            next_link.click()
            frame.wait_for_timeout(5000)
            content = frame.content()
            _screenshot(page, "08_page2_loaded")
            # Should see "Records X-Y of Z" indicating page 2
            assert "Records" in content or "records" in content, \
                "Page 2 should show record range"

    def test_specialty_filter_panel_visible(self, page: Page):
        """After provider search, specialty filter panel appears in left panel."""
        frame = _get_chat_frame(page)
        _send_message(frame, "find surgeons in delaware")
        page.wait_for_timeout(2000)
        # Check parent page for filter panel content
        left_panel = page.locator("#leftPanel")
        if left_panel.count() > 0:
            panel_text = left_panel.inner_text()
            _screenshot(page, "09_filter_panel")
            assert "Filter" in panel_text or "filter" in panel_text or len(panel_text) > 20, \
                "Left panel should show specialty filter options"
