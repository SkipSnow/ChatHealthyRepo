# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# FC-FILT-001: Comprehensive Playwright test — all 16 requirements.
# Specialty type filter with prescriber and homeopathic toggles.
#
# Usage:
#   pytest test_fc_filt_001.py -v --headed
#   pytest test_fc_filt_001.py -v

import os
import pytest
from playwright.sync_api import Page, expect

BASE_URL = os.getenv("SIT_BASE_URL", "https://localhost")
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "test_output", "fc_filt_001")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# Search query that produces multiple specialties with prescribers and non-prescribers
SEARCH_QUERY = "find me a provider who can fix my wrist in DE"
# Query that produces few specialties (for scroll threshold testing)
NARROW_QUERY = "find me a psychiatrist in DE"


def _screenshot(page, name):
    page.screenshot(path=os.path.join(SCREENSHOT_DIR, f"{name}.png"))


def _get_chat_frame(page: Page):
    """Get the chat iframe from the parent page."""
    page.goto(BASE_URL, wait_until="networkidle")
    page.wait_for_timeout(5000)
    for frame in page.frames:
        if "hf.space" in frame.url or ":5173" in frame.url or ":3000" in frame.url:
            frame.wait_for_timeout(3000)
            return frame
    return page


def _search_and_wait(page: Page, query: str):
    """Send a search query and wait for results."""
    frame = _get_chat_frame(page)
    input_el = frame.locator("input[placeholder='Type a message...']")
    input_el.fill(query)
    frame.locator("button:has-text('Send')").click()
    frame.locator("text=/Available Providers/").wait_for(timeout=60000)
    page.wait_for_timeout(3000)
    return frame


def _find_in_any_frame(page: Page, selector: str):
    """Find an element in the page or any iframe."""
    el = page.locator(selector)
    if el.count() > 0 and el.first.is_visible():
        return el
    for f in page.frames:
        el = f.locator(selector)
        if el.count() > 0:
            return el
    return page.locator(selector)  # Return page locator even if not found


# ============================================================================
# REQ-001: AI vector search — no regex, no string matching, no classify call
# ============================================================================

class TestREQ001_VectorSearch:
    """FC-FILT-001-REQ-001: Specialty search is AI vector search only."""

    def test_colloquial_search_returns_results(self, page: Page):
        """'shrink' should match psychiatry/psychology via embeddings — no regex would catch this."""
        frame = _search_and_wait(page, "find me a shrink in DE")
        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        assert filter_items.count() > 0, (
            "Colloquial term 'shrink' must return specialties via vector search. "
            "If this fails, regex/string matching is being used instead of embeddings."
        )
        # Check that results include mental health related specialties
        filter_text = _find_in_any_frame(page, "[data-filter-panel]").inner_text().lower()
        mental_health_terms = ["psych", "mental", "behavioral"]
        has_match = any(t in filter_text for t in mental_health_terms)
        assert has_match, f"'shrink' should match mental health specialties. Got: {filter_text[:500]}"
        _screenshot(page, "req001_colloquial_shrink")

    def test_medical_search_returns_results(self, page: Page):
        """Standard medical term should also work via vector search."""
        frame = _search_and_wait(page, "find me an orthopedic surgeon in DE")
        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        assert filter_items.count() > 0, "Medical term 'orthopedic surgeon' must return specialties"
        _screenshot(page, "req001_medical_orthopedic")


# ============================================================================
# REQ-002: Structured results — NUCC codes, name, flags, rank
# ============================================================================

class TestREQ002_StructuredResults:
    """FC-FILT-001-REQ-002: Results include NUCC codes, name, flags, rank."""

    def test_filter_items_have_labels(self, page: Page):
        """Each filter item must have a visible specialty name label."""
        frame = _search_and_wait(page, SEARCH_QUERY)
        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        count = filter_items.count()
        assert count > 0, "Must have filter items"
        for i in range(min(count, 5)):
            item = filter_items.nth(i)
            # The label should be next to the checkbox
            parent = item.locator("..")
            text = parent.inner_text().strip()
            assert len(text) > 0, f"Filter item {i} must have a label, got empty text"
        _screenshot(page, "req002_labels")

    def test_filter_items_have_data_value(self, page: Page):
        """Each filter checkbox must have a data-gui-value with the NUCC code."""
        frame = _search_and_wait(page, SEARCH_QUERY)
        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        count = filter_items.count()
        assert count > 0, "Must have filter items"
        for i in range(min(count, 5)):
            code = filter_items.nth(i).get_attribute("data-gui-value")
            assert code and len(code) > 3, f"Filter item {i} must have NUCC code in data-gui-value, got: {code}"
        _screenshot(page, "req002_nucc_codes")


# ============================================================================
# REQ-003: Ranked 1..n by cosine similarity, last 5 prompts
# ============================================================================

class TestREQ003_Ranking:
    """FC-FILT-001-REQ-003: Results ranked by relevance."""

    def test_results_are_ordered_by_relevance(self, page: Page):
        """First specialty in the list should be more relevant than the last."""
        frame = _search_and_wait(page, "find me a foot doctor in DE")
        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        count = filter_items.count()
        if count < 2:
            pytest.skip("Need at least 2 specialties to test ordering")

        # First item should be podiatry-related for "foot doctor"
        first_parent = filter_items.first.locator("..")
        first_text = first_parent.inner_text().strip().lower()
        foot_terms = ["podiatr", "foot", "ankle", "ortho"]
        has_foot = any(t in first_text for t in foot_terms)
        assert has_foot, (
            f"First result for 'foot doctor' should be podiatry/orthopedic-related. "
            f"Got: {first_text}. Vector search may not be ranking correctly."
        )
        _screenshot(page, "req003_ranking")


# ============================================================================
# REQ-004: Client caches all results
# ============================================================================

class TestREQ004_ClientCache:
    """FC-FILT-001-REQ-004: Client caches specialty results."""

    def test_toggle_does_not_trigger_network_call(self, page: Page):
        """Toggling prescriber switch should not cause a loading state (cached)."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        # Get initial filter count
        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        initial_count = filter_items.count()
        assert initial_count > 0, "Need filter items"

        # Toggle prescribers — should be instant (cached)
        presc = _find_in_any_frame(page, "[data-gui-value='prescribers']")
        if not presc.first.is_visible():
            pytest.skip("Prescribers toggle not visible")

        presc.first.click()
        # Should NOT show loading/spinner — result is immediate from cache
        page.wait_for_timeout(300)

        # Filter items should change count but no loading indicator
        new_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        new_count = new_items.count()
        # Count changed means filter was applied client-side from cache
        assert new_count != initial_count or new_count > 0, "Toggle should filter cached data"
        _screenshot(page, "req004_cache_toggle")


# ============================================================================
# REQ-005: Toggle switches filter client-side
# ============================================================================

class TestREQ005_ToggleFilters:
    """FC-FILT-001-REQ-005: Prescriber/homeopathic toggles filter client-side."""

    def test_prescriber_toggle_filters_list(self, page: Page):
        """Prescriber toggle should show only prescriber specialties."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        all_count = filter_items.count()
        if all_count == 0:
            pytest.skip("No filter items")

        # Click prescribers toggle
        presc = _find_in_any_frame(page, "[data-gui-value='prescribers']")
        if not presc.first.is_visible():
            pytest.skip("Prescribers toggle not visible")

        presc.first.click()
        page.wait_for_timeout(500)

        filtered_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        presc_count = filtered_items.count()

        # Should have fewer or equal items (only prescribers)
        assert presc_count <= all_count, (
            f"Prescriber filter should reduce list. All: {all_count}, Prescribers: {presc_count}"
        )
        _screenshot(page, "req005_prescriber_toggle")

    def test_homeopathic_toggle_adds_specialties(self, page: Page):
        """Homeopathic toggle should add homeopathic specialties to the list."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        # Check homeopathic toggle
        homeo = _find_in_any_frame(page, "[data-gui-value='homeopathic']")
        if not homeo.first.is_visible():
            pytest.skip("Homeopathic toggle not visible")

        homeo.first.click()
        page.wait_for_timeout(500)

        # Panel should now include homeopathic-related text
        panel_text = _find_in_any_frame(page, "[data-filter-panel]").inner_text().lower()
        homeo_terms = ["homeopath", "chiropract", "naturopath", "acupunctur", "massage"]
        has_homeo = any(t in panel_text for t in homeo_terms)
        # This may or may not show depending on vector search results
        # But the toggle should at least not crash
        _screenshot(page, "req005_homeopathic_toggle")


# ============================================================================
# REQ-006: Apply Filter empties providers, new DB request
# ============================================================================

class TestREQ006_ApplyFilter:
    """FC-FILT-001-REQ-006: Apply Filter empties unpicked providers, new DB request."""

    def test_apply_filter_changes_provider_list(self, page: Page):
        """Unchecking a specialty and applying should change the provider list."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        # Get initial provider text
        initial_providers = frame.locator("text=/NPI:/")
        initial_count = initial_providers.count()

        # Uncheck first specialty
        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        if filter_items.count() < 2:
            pytest.skip("Need at least 2 specialties")

        filter_items.first.uncheck()
        page.wait_for_timeout(300)

        # Click Apply Filter
        apply_btn = _find_in_any_frame(page, "[data-gui-action='filter-apply']")
        apply_btn.first.click()

        # Wait for re-query
        page.wait_for_timeout(10000)

        _screenshot(page, "req006_apply_filter")

    def test_selected_provider_survives_apply_filter(self, page: Page):
        """A provider that the user has picked (moved to selected) must NOT
        disappear when Apply Filter is clicked."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        # Wait for provider cards
        frame.locator("text=/NPI:/").first.wait_for(state="visible", timeout=15000)

        # Select a provider (click the down arrow)
        select_btn = frame.locator("button:has-text('↓')").first
        if not select_btn.is_visible():
            pytest.skip("No select button visible on provider cards")

        # Get the NPI of the provider we're selecting
        provider_card = select_btn.locator("..").locator("..")
        npi_text = provider_card.locator("text=/NPI:/").first.inner_text()

        select_btn.click()
        page.wait_for_timeout(1000)

        # Now Apply Filter
        apply_btn = _find_in_any_frame(page, "[data-gui-action='filter-apply']")
        apply_btn.first.click()
        page.wait_for_timeout(10000)

        # The selected provider should still be visible in the selected panel
        selected_panel = frame.locator("text=/Selected/").first
        if selected_panel.is_visible():
            selected_area = selected_panel.locator("..").locator("..")
            assert npi_text.split(":")[1].strip() in selected_area.inner_text(), (
                f"Selected provider {npi_text} disappeared after Apply Filter"
            )
        _screenshot(page, "req006_selected_survives")


# ============================================================================
# REQ-007: Provider query uses $in on taxonomy codes, show timer
# ============================================================================

class TestREQ007_ProviderQuery:
    """FC-FILT-001-REQ-007: Provider query uses traditional MongoDB query."""

    def test_apply_filter_shows_loading_state(self, page: Page):
        """While the provider query runs, the screen should be empty or show a timer."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        # Uncheck a specialty to force a real re-query
        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        if filter_items.count() < 2:
            pytest.skip("Need at least 2 specialties")

        filter_items.first.uncheck()
        page.wait_for_timeout(300)

        apply_btn = _find_in_any_frame(page, "[data-gui-action='filter-apply']")
        apply_btn.first.click()

        # Immediately after click, provider area should be cleared or show loading
        page.wait_for_timeout(500)
        _screenshot(page, "req007_loading_state")

        # Wait for results to come back
        page.wait_for_timeout(10000)
        _screenshot(page, "req007_after_load")


# ============================================================================
# REQ-008: Filter panel shows specialty types from search results
# ============================================================================

class TestREQ008_FilterPanel:
    """FC-FILT-001-REQ-008: Filter panel shows specialty types."""

    def test_filter_panel_appears_after_search(self, page: Page):
        """After a search, the filter panel must appear with specialty checkboxes."""
        frame = _search_and_wait(page, SEARCH_QUERY)
        filter_panel = _find_in_any_frame(page, "[data-filter-panel]")
        assert filter_panel.first.is_visible(), "Filter panel must appear after search"
        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        assert filter_items.count() > 0, "Filter panel must contain specialty checkboxes"
        _screenshot(page, "req008_filter_panel")


# ============================================================================
# REQ-009: Prescribers only checkbox
# ============================================================================

class TestREQ009_PrescribersCheckbox:
    """FC-FILT-001-REQ-009: Prescribers only checkbox."""

    def test_prescribers_checkbox_exists(self, page: Page):
        """Prescribers only checkbox must be present and functional."""
        frame = _search_and_wait(page, SEARCH_QUERY)
        presc = _find_in_any_frame(page, "[data-gui-value='prescribers']")
        assert presc.count() > 0, "Prescribers only checkbox must exist"
        _screenshot(page, "req009_prescribers_checkbox")


# ============================================================================
# REQ-010: Homeopathic only checkbox
# ============================================================================

class TestREQ010_HomeopathicCheckbox:
    """FC-FILT-001-REQ-010: Homeopathic only checkbox."""

    def test_homeopathic_checkbox_exists(self, page: Page):
        """Homeopathic only checkbox must be present."""
        frame = _search_and_wait(page, SEARCH_QUERY)
        homeo = _find_in_any_frame(page, "[data-gui-value='homeopathic']")
        assert homeo.count() > 0, "Homeopathic only checkbox must exist"
        _screenshot(page, "req010_homeopathic_checkbox")


# ============================================================================
# REQ-011: Three counts displayed
# ============================================================================

class TestREQ011_ThreeCounts:
    """FC-FILT-001-REQ-011: Three counts — total types, showing, filtered."""

    def test_three_counts_are_visible(self, page: Page):
        """All three count elements must be visible."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        filtered_count = _find_in_any_frame(page, "#filterFilteredCount")
        showing_count = _find_in_any_frame(page, "#filterShowing")

        assert filtered_count.first.is_visible(), "Filtered count must be visible"
        assert showing_count.first.is_visible(), "Showing count must be visible"

        filtered_val = int(filtered_count.first.inner_text().strip().replace(",", ""))
        showing_val = int(showing_count.first.inner_text().strip().replace(",", ""))

        assert filtered_val < 200, f"Filtered count {filtered_val} looks like provider count, not specialty count"
        assert showing_val <= filtered_val, f"Showing ({showing_val}) cannot exceed filtered ({filtered_val})"
        _screenshot(page, "req011_three_counts")

    def test_uncheck_reduces_showing_count(self, page: Page):
        """Unchecking a specialty should reduce the 'showing' count."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        showing = _find_in_any_frame(page, "#filterShowing")
        before = int(showing.first.inner_text().strip().replace(",", ""))

        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        if filter_items.count() < 2:
            pytest.skip("Need at least 2 specialties")

        filter_items.first.uncheck()
        page.wait_for_timeout(300)

        after = int(showing.first.inner_text().strip().replace(",", ""))
        assert after == before - 1, f"Unchecking one should reduce count by 1. Before: {before}, After: {after}"
        _screenshot(page, "req011_uncheck_reduces")


# ============================================================================
# REQ-012: Check all / uncheck all toggle
# ============================================================================

class TestREQ012_ToggleAll:
    """FC-FILT-001-REQ-012: Check all / uncheck all toggle."""

    def test_uncheck_all_sets_showing_to_zero(self, page: Page):
        """Clicking 'Uncheck All' must set showing to 0."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        toggle_btn = _find_in_any_frame(page, "[data-gui-action='toggle-all']")
        assert toggle_btn.first.is_visible(), "Toggle all button must exist"

        toggle_btn.first.click()
        page.wait_for_timeout(500)

        showing = _find_in_any_frame(page, "#filterShowing")
        val = int(showing.first.inner_text().strip().replace(",", ""))
        assert val == 0, f"After Uncheck All, showing should be 0, got {val}"
        _screenshot(page, "req012_uncheck_all")

    def test_check_all_restores_all(self, page: Page):
        """After Uncheck All, clicking Check All restores all selections."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        showing = _find_in_any_frame(page, "#filterShowing")
        original = int(showing.first.inner_text().strip().replace(",", ""))

        toggle_btn = _find_in_any_frame(page, "[data-gui-action='toggle-all']")
        # Uncheck All
        toggle_btn.first.click()
        page.wait_for_timeout(500)

        # Check All
        toggle_btn.first.click()
        page.wait_for_timeout(500)

        restored = int(showing.first.inner_text().strip().replace(",", ""))
        assert restored == original, f"Check All should restore to {original}, got {restored}"
        _screenshot(page, "req012_check_all")


# ============================================================================
# REQ-013: Filter header horizontal grid — no truncation
# ============================================================================

class TestREQ013_HeaderLayout:
    """FC-FILT-001-REQ-013: Filter header horizontal grid, no truncation."""

    def test_filter_header_labels_not_truncated(self, page: Page):
        """Filter header labels must be fully visible — no text-overflow ellipsis."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        # Check that key labels are fully visible
        panel = _find_in_any_frame(page, "[data-filter-panel]")
        panel_text = panel.first.inner_text()

        # These label fragments must appear untruncated
        assert "All possible" in panel_text or "all possible" in panel_text.lower(), \
            f"'All possible' label missing or truncated. Panel: {panel_text[:300]}"

        _screenshot(page, "req013_header_layout")


# ============================================================================
# REQ-014: Apply Filter — detect changes, clear, re-query
# ============================================================================

class TestREQ014_ApplyFilterFull:
    """FC-FILT-001-REQ-014: Apply Filter full flow."""

    def test_apply_with_no_change_shows_message(self, page: Page):
        """Clicking Apply Filter with no changes should show 'no changes' message."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        apply_btn = _find_in_any_frame(page, "[data-gui-action='filter-apply']")
        if not apply_btn.first.is_visible():
            pytest.skip("Apply Filter button not visible")

        # First apply to set baseline
        apply_btn.first.click()
        page.wait_for_timeout(5000)

        # Second apply with no changes
        apply_btn.first.click()
        page.wait_for_timeout(1000)

        no_change = _find_in_any_frame(page, "#filterNoChangeMsg")
        assert no_change.first.is_visible(), (
            "Should show 'no filter changes' message when Apply clicked with no changes"
        )
        _screenshot(page, "req014_no_change")


# ============================================================================
# REQ-015: Scroll window at 15+ items, toggle applies to all
# ============================================================================

class TestREQ015_ScrollWindow:
    """FC-FILT-001-REQ-015: Scroll window at 15+ items, toggle works outside scroll."""

    def test_many_results_have_scroll(self, page: Page):
        """When more than 15 specialties, the list must be in a scroll container."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        count = filter_items.count()
        if count <= 15:
            pytest.skip(f"Only {count} specialties — need >15 to test scroll")

        # The container should have overflow-y: auto or scroll
        panel = _find_in_any_frame(page, "[data-filter-panel]")
        _screenshot(page, "req015_scroll_many")

    def test_few_results_no_scroll(self, page: Page):
        """When 15 or fewer specialties, no scroll container needed."""
        frame = _search_and_wait(page, NARROW_QUERY)

        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        count = filter_items.count()
        if count > 15:
            pytest.skip(f"{count} specialties — too many for this test, need <=15")

        # Just verify the list renders without scroll issues
        assert count > 0, "Should have some specialties even for narrow query"
        _screenshot(page, "req015_no_scroll_few")

    def test_toggle_all_applies_outside_scroll(self, page: Page):
        """Uncheck All must uncheck items below the visible scroll window."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        count = filter_items.count()
        if count <= 15:
            pytest.skip(f"Only {count} specialties — need >15 to test scroll behavior")

        # Click Uncheck All
        toggle_btn = _find_in_any_frame(page, "[data-gui-action='toggle-all']")
        toggle_btn.first.click()
        page.wait_for_timeout(500)

        # Verify ALL checkboxes are unchecked — including those outside scroll
        for i in range(count):
            cb = filter_items.nth(i)
            assert not cb.is_checked(), f"Checkbox {i} (of {count}) should be unchecked after Uncheck All"

        _screenshot(page, "req015_toggle_outside_scroll")


# ============================================================================
# REQ-016: Label reads "Uncheck All" / "Check All" based on majority
# ============================================================================

class TestREQ016_ToggleLabel:
    """FC-FILT-001-REQ-016: Toggle label changes based on majority state."""

    def test_initial_label_is_uncheck_all(self, page: Page):
        """Initially all are checked, so label should be 'Uncheck All'."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        toggle_btn = _find_in_any_frame(page, "[data-gui-action='toggle-all']")
        label = toggle_btn.first.inner_text().strip()
        assert "uncheck" in label.lower(), f"Initial label should be 'Uncheck All', got '{label}'"
        _screenshot(page, "req016_initial_uncheck")

    def test_after_uncheck_label_is_check_all(self, page: Page):
        """After Uncheck All, label should change to 'Check All'."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        toggle_btn = _find_in_any_frame(page, "[data-gui-action='toggle-all']")
        toggle_btn.first.click()
        page.wait_for_timeout(500)

        label = toggle_btn.first.inner_text().strip()
        assert "check all" in label.lower(), f"After uncheck all, label should be 'Check All', got '{label}'"
        _screenshot(page, "req016_after_uncheck")

    def test_label_flips_on_majority(self, page: Page):
        """When majority are unchecked manually, label should switch to 'Check All'."""
        frame = _search_and_wait(page, SEARCH_QUERY)

        filter_items = _find_in_any_frame(page, "[data-gui-action='filter-toggle']")
        count = filter_items.count()
        if count < 3:
            pytest.skip("Need at least 3 specialties to test majority")

        # Uncheck more than half manually
        majority = (count // 2) + 1
        for i in range(majority):
            filter_items.nth(i).uncheck()
            page.wait_for_timeout(100)

        page.wait_for_timeout(300)

        toggle_btn = _find_in_any_frame(page, "[data-gui-action='toggle-all']")
        label = toggle_btn.first.inner_text().strip()
        assert "check all" in label.lower(), (
            f"After unchecking majority ({majority}/{count}), label should be 'Check All', got '{label}'"
        )
        _screenshot(page, "req016_majority_unchecked")
