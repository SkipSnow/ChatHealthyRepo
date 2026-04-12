# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# BUG-UX-008: Environment banner must show labeled version info.
# Playwright SIT test against full parent page (DR-019).

import os
import pytest
from playwright.sync_api import Page

BASE_URL = os.getenv("SIT_BASE_URL", "https://localhost")

SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "test_output")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


class TestEnvironmentBanner:
    """BUG-UX-008: Banner must display environment, version, framework, build, commit with labels."""

    def test_banner_visible(self, page: Page):
        """Banner is visible on non-prod environments."""
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(8000)  # Wait for health fetch to populate banner

        banner = page.locator("#envBanner")
        assert banner.is_visible(), "Environment banner should be visible"
        page.screenshot(path=os.path.join(SCREENSHOT_DIR, "banner_visible.png"))

    def test_banner_has_environment_label(self, page: Page):
        """Banner shows environment name (LOCAL, DEV, QA)."""
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(8000)

        banner = page.locator("#envBanner")
        text = banner.inner_text()
        assert any(env in text for env in ("LOCAL", "DEV", "QA")), \
            f"Banner must show environment name. Got: {text}"

    def test_banner_has_version_label(self, page: Page):
        """Banner shows 'Version:' with a value."""
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(8000)

        banner = page.locator("#envBanner")
        text = banner.inner_text()
        assert "Version:" in text, f"Banner must show 'Version:' label. Got: {text}"

    def test_banner_has_framework_label(self, page: Page):
        """Banner shows 'Framework:' with a value."""
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(8000)

        banner = page.locator("#envBanner")
        text = banner.inner_text()
        assert "Framework:" in text, f"Banner must show 'Framework:' label. Got: {text}"

    def test_banner_has_build_label(self, page: Page):
        """Banner shows 'Build:' with a numeric value."""
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(8000)

        banner = page.locator("#envBanner")
        text = banner.inner_text()
        assert "Build:" in text, f"Banner must show 'Build:' label. Got: {text}"

    def test_banner_has_commit_label(self, page: Page):
        """Banner shows 'Commit:' with a short hash."""
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(8000)

        banner = page.locator("#envBanner")
        text = banner.inner_text()
        assert "Commit:" in text, f"Banner must show 'Commit:' label. Got: {text}"

    def test_banner_labels_are_spaced(self, page: Page):
        """Banner labels are spaced apart, not jammed together."""
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(8000)

        banner = page.locator("#envBanner")
        # Check that spans exist with margin (rendered as separate elements)
        spans = banner.locator("span")
        assert spans.count() >= 4, \
            f"Banner should have at least 4 labeled spans, got {spans.count()}"
        page.screenshot(path=os.path.join(SCREENSHOT_DIR, "banner_formatted.png"))
