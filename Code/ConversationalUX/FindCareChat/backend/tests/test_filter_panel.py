# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# BUG-BA-001 / UAT-FILTER-001: Filter panel three-number labels display correctly.
# Playwright SIT test against full parent page (DR-019).
#
# The three numbers must be:
#   1. All possible — total specialty types returned by classify
#   2. Prescribers — specialty types that are prescribers
#   3. Your choices — specialty types the user has checked
#
# Numbers must be specialty TYPE counts, never provider counts.
#
# Usage:
#   pytest test_filter_panel.py -v --headed
#   pytest test_filter_panel.py -v

import os
import re
import pytest
from playwright.sync_api import Page, expect

BASE_URL = os.getenv("SIT_BASE_URL", "https://localhost")

SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "test_screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


def _get_chat_frame(page: Page):
    """Get the chat iframe from the parent page."""
    page.goto(BASE_URL, wait_until="networkidle")
    page.wait_for_timeout(5000)
    for frame in page.frames:
        if "hf.space" in frame.url or ":5173" in frame.url or ":3000" in frame.url:
            frame.wait_for_timeout(3000)
            return frame
    return page


def _get_left_panel(page: Page):
    """Get the left panel from the parent page (filter lives here)."""
    page.goto(BASE_URL, wait_until="networkidle")
    page.wait_for_timeout(5000)
    return page


class TestFilterPanelDisplay:
    """BUG-BA-001 / UAT-FILTER-001: Filter panel displays correct specialty type counts."""

    def test_filter_header_shows_specialty_counts_not_provider_counts(self, page: Page):
        """Three numbers in filter header must be specialty type counts.
        BUG-BA-001: First number was showing provider count (4,676) instead of
        specialty type count. Must be fixed."""
        frame = _get_chat_frame(page)

        # Search for something that returns multiple specialties
        input_el = frame.locator("input[placeholder='Type a message...']")
        input_el.fill("find me a provider who can fix my wrist in DE")
        frame.locator("button:has-text('Send')").click()

        # Wait for results
        frame.locator("text=/Available Providers/").wait_for(timeout=30000)

        # Wait for filter panel to appear in parent page left panel
        page.wait_for_timeout(3000)

        # Filter panel is in the parent page, not the iframe
        filter_panel = page.locator("[data-filter-panel]")
        if not filter_panel.is_visible():
            # Filter may be inside an iframe — check all frames
            for f in page.frames:
                filter_panel = f.locator("[data-filter-panel]")
                if filter_panel.count() > 0:
                    break

        if not filter_panel.is_visible():
            pytest.skip("Filter panel not visible — may need more than 1 specialty to trigger")

        # Get the three numbers
        all_possible = page.locator("text=/All possible/").locator("..").locator("div:nth-child(2)")
        prescribers = page.locator("#filterFilteredCount")
        your_choices = page.locator("#filterShowing")

        all_val = int(all_possible.text_content().strip().replace(",", ""))
        presc_val = int(prescribers.text_content().strip().replace(",", ""))
        choice_val = int(your_choices.text_content().strip().replace(",", ""))

        # All three must be small numbers (specialty type counts, not provider counts)
        # Provider counts would be in hundreds or thousands
        assert all_val < 100, (
            f"'All possible' shows {all_val} — looks like a provider count, not specialty type count. "
            f"BUG-BA-001: must show specialty TYPE count."
        )
        assert presc_val <= all_val, (
            f"Prescribers ({presc_val}) cannot exceed All possible ({all_val})"
        )
        assert choice_val <= all_val, (
            f"Your choices ({choice_val}) cannot exceed All possible ({all_val})"
        )

        # Initially, Your choices should equal Prescribers (all checked by default)
        assert choice_val == presc_val, (
            f"Initial 'Your choices' ({choice_val}) should equal 'Prescribers' ({presc_val}) "
            f"since all are checked by default"
        )

        page.screenshot(path=os.path.join(SCREENSHOT_DIR, "filter_panel_numbers.png"))

    def test_filter_checkboxes_visible_and_labeled(self, page: Page):
        """Each specialty in the filter must have a visible checkbox with a name label."""
        frame = _get_chat_frame(page)

        input_el = frame.locator("input[placeholder='Type a message...']")
        input_el.fill("find me a provider who can fix my wrist in DE")
        frame.locator("button:has-text('Send')").click()

        frame.locator("text=/Available Providers/").wait_for(timeout=30000)
        page.wait_for_timeout(3000)

        # Find filter checkboxes
        checkboxes = page.locator("[data-gui-action='filter-toggle']")
        if checkboxes.count() == 0:
            for f in page.frames:
                checkboxes = f.locator("[data-gui-action='filter-toggle']")
                if checkboxes.count() > 0:
                    break

        if checkboxes.count() == 0:
            pytest.skip("No filter checkboxes found — may need more than 1 specialty")

        count = checkboxes.count()
        assert count > 0, "Filter must have at least one specialty checkbox"
        assert count < 100, f"Filter has {count} checkboxes — sanity check"

        # Each checkbox must be checked by default
        for i in range(count):
            cb = checkboxes.nth(i)
            assert cb.is_checked(), f"Checkbox {i} should be checked by default"

        page.screenshot(path=os.path.join(SCREENSHOT_DIR, "filter_checkboxes.png"))

    def test_uncheck_all_updates_your_choices_to_zero(self, page: Page):
        """Clicking 'Uncheck All' must set 'Your choices' to 0."""
        frame = _get_chat_frame(page)

        input_el = frame.locator("input[placeholder='Type a message...']")
        input_el.fill("find me a provider who can fix my wrist in DE")
        frame.locator("button:has-text('Send')").click()

        frame.locator("text=/Available Providers/").wait_for(timeout=30000)
        page.wait_for_timeout(3000)

        # Click Uncheck All
        uncheck_btn = page.locator("[data-gui-action='toggle-all']")
        if not uncheck_btn.is_visible():
            for f in page.frames:
                uncheck_btn = f.locator("[data-gui-action='toggle-all']")
                if uncheck_btn.count() > 0:
                    break

        if not uncheck_btn.is_visible():
            pytest.skip("Uncheck All button not visible")

        uncheck_btn.click()
        page.wait_for_timeout(500)

        your_choices = page.locator("#filterShowing")
        if not your_choices.is_visible():
            for f in page.frames:
                your_choices = f.locator("#filterShowing")
                if your_choices.count() > 0:
                    break

        choice_val = int(your_choices.text_content().strip().replace(",", ""))
        assert choice_val == 0, f"After 'Uncheck All', Your choices should be 0, got {choice_val}"

        page.screenshot(path=os.path.join(SCREENSHOT_DIR, "filter_uncheck_all.png"))

    def test_prescribers_only_filters_to_prescriber_specialties(self, page: Page):
        """FC-FILT-001-REQ-002: Prescribers only checkbox filters to prescriber specialties."""
        frame = _get_chat_frame(page)

        input_el = frame.locator("input[placeholder='Type a message...']")
        input_el.fill("find me a provider who can fix my wrist in DE")
        frame.locator("button:has-text('Send')").click()

        frame.locator("text=/Available Providers/").wait_for(timeout=30000)
        page.wait_for_timeout(3000)

        # Find prescribers-only checkbox
        prescriber_cb = page.locator("[data-gui-action='prescribers-only']")
        if not prescriber_cb.is_visible():
            for f in page.frames:
                prescriber_cb = f.locator("[data-gui-action='prescribers-only']")
                if prescriber_cb.count() > 0:
                    break

        if not prescriber_cb.is_visible():
            pytest.skip("Prescribers only checkbox not visible")

        # Get count before
        your_choices_before = page.locator("#filterShowing")
        before_val = int(your_choices_before.text_content().strip().replace(",", ""))

        # Click prescribers only
        prescriber_cb.click()
        page.wait_for_timeout(500)

        your_choices_after = page.locator("#filterShowing")
        after_val = int(your_choices_after.text_content().strip().replace(",", ""))

        # Prescribers-only should reduce or change the count
        assert after_val <= before_val, \
            f"Prescribers only should filter down. Before: {before_val}, After: {after_val}"

        page.screenshot(path=os.path.join(SCREENSHOT_DIR, "filter_prescribers_only.png"))

    def test_homeopathic_only_adds_homeopathic_specialties(self, page: Page):
        """FC-FILT-001-REQ-003: Homeopathic only checkbox adds homeopathic specialties."""
        frame = _get_chat_frame(page)

        input_el = frame.locator("input[placeholder='Type a message...']")
        input_el.fill("find me a provider who can fix my wrist in DE")
        frame.locator("button:has-text('Send')").click()

        frame.locator("text=/Available Providers/").wait_for(timeout=30000)
        page.wait_for_timeout(3000)

        # Find homeopathic checkbox
        homeo_cb = page.locator("[data-gui-action='homeopathic-only']")
        if not homeo_cb.is_visible():
            for f in page.frames:
                homeo_cb = f.locator("[data-gui-action='homeopathic-only']")
                if homeo_cb.count() > 0:
                    break

        if not homeo_cb.is_visible():
            pytest.skip("Homeopathic only checkbox not visible")

        # Click homeopathic only
        homeo_cb.click()
        page.wait_for_timeout(500)

        # Check that the filter panel text now includes homeopathic-related content
        filter_text = page.locator("#leftPanel").inner_text()
        has_homeo = "homeopath" in filter_text.lower() or "naturopath" in filter_text.lower()
        assert has_homeo, \
            f"Homeopathic checkbox should add homeopathic specialties. Panel: {filter_text[:300]}"

        page.screenshot(path=os.path.join(SCREENSHOT_DIR, "filter_homeopathic.png"))
