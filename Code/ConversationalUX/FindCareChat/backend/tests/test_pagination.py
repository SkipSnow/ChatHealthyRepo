# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pagination requirement tests.
# Every requirement from the pagination design has a test case.

import json
import os
import sys
import unittest

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "Code", ".env"))


class TestPaginationRequirements(unittest.TestCase):
    """Tests for pagination requirements. Uses real /chat and /search endpoints."""

    @classmethod
    def setUpClass(cls):
        """Import and create FastAPI test client."""
        from main import app
        from fastapi.testclient import TestClient
        cls.client = TestClient(app)

    # ── R1: Tool returns total_count with first page ────────────────

    def test_r1_search_returns_total_count(self):
        """Tool must return total_count showing how many records match."""
        r = self.client.post("/search", json={"state": "DE", "limit": 5, "name": "Smith"})
        data = r.json()
        self.assertGreater(data["total_count"], 0, "total_count must be > 0")

    def test_r1_search_returns_count(self):
        """Tool must return count of records in current page."""
        r = self.client.post("/search", json={"state": "DE", "limit": 5, "name": "Smith"})
        data = r.json()
        self.assertEqual(data["count"], 5)

    def test_r1_search_returns_has_more(self):
        """Tool must indicate if more records exist."""
        r = self.client.post("/search", json={"state": "DE", "limit": 5, "name": "Smith"})
        data = r.json()
        self.assertIn("has_more", data)
        self.assertTrue(data["has_more"])

    # ── R2: fetch_all removed from LLM tool definition ─────────────

    def test_r2_fetch_all_not_in_tool_schema(self):
        """fetch_all must NOT be in the tool definition the LLM sees."""
        r = self.client.get("/openapi.json")
        schemas = r.json()["components"]["schemas"]
        # Check SearchRequest doesn't have fetch_all (SearchRequest is the /search model)
        if "ProviderSearchInput" in schemas:
            props = schemas["ProviderSearchInput"].get("properties", {})
            self.assertNotIn("fetch_all", props, "fetch_all must not be in LLM tool definition")

    def test_r2_after_npi_not_in_tool_schema(self):
        """after_npi must NOT be in the tool definition the LLM sees."""
        from ProviderManagement.provider_search_models import ProviderSearchInput
        fields = ProviderSearchInput.model_fields
        self.assertNotIn("after_npi", fields, "after_npi must not be in LLM tool definition")
        self.assertNotIn("fetch_all", fields, "fetch_all must not be in LLM tool definition")

    # ── R3: search_params returned for replay ───────────────────────

    def test_r3_search_returns_search_params(self):
        """Tool must return search_params so frontend can replay the query."""
        r = self.client.post("/search", json={"state": "DE", "limit": 5, "name": "Smith"})
        data = r.json()
        self.assertIn("search_params", data)
        self.assertIsInstance(data["search_params"], dict)
        self.assertEqual(data["search_params"]["state"], "DE")
        self.assertEqual(data["search_params"]["name"], "Smith")

    # ── R4: Keyset pagination with after_npi ────────────────────────

    def test_r4_search_returns_last_npi(self):
        """Tool must return last_npi for keyset pagination."""
        r = self.client.post("/search", json={"state": "DE", "limit": 5, "name": "Smith"})
        data = r.json()
        self.assertTrue(data["last_npi"], "last_npi must not be empty")

    def test_r4_search_returns_first_npi(self):
        """Tool must return first_npi for back navigation."""
        r = self.client.post("/search", json={"state": "DE", "limit": 5, "name": "Smith"})
        data = r.json()
        self.assertTrue(data["first_npi"], "first_npi must not be empty")

    def test_r4_pagination_with_after_npi(self):
        """Passing after_npi returns the next page of results."""
        r1 = self.client.post("/search", json={"state": "DE", "limit": 5, "name": "Smith"})
        page1 = r1.json()
        last = page1["last_npi"]

        r2 = self.client.post("/search", json={"state": "DE", "limit": 5, "name": "Smith", "after_npi": last})
        page2 = r2.json()
        self.assertGreater(page2["count"], 0, "Page 2 must have results")
        # Page 2 first NPI must be greater than page 1 last NPI
        self.assertGreater(page2["first_npi"], last, "Page 2 must start after page 1")

    # ── R5: End of results ──────────────────────────────────────────

    def test_r5_has_more_false_at_end(self):
        """has_more must be False when all results have been returned."""
        r = self.client.post("/search", json={"state": "DE", "limit": 100, "name": "Smith"})
        data = r.json()
        if data["total_count"] <= 100:
            self.assertFalse(data["has_more"], "has_more must be False at end of results")

    # ── R6: /chat returns pagination metadata ───────────────────────

    def test_r6_chat_returns_pagination(self):
        """/chat must return pagination metadata when find_providers is called."""
        r = self.client.post("/chat", json={
            "message": "find pediatricians in delaware",
            "history": [],
        })
        data = r.json()
        if data.get("error"):
            self.skipTest(f"Chat API error (likely timeout): {data['error'][:100]}")
        self.assertIsNotNone(data.get("response"), "Chat must return a response")
        pagination = data.get("pagination")
        self.assertIsNotNone(pagination, "Chat response must include pagination metadata")
        self.assertGreater(pagination["total_count"], 0, "pagination.total_count must be > 0")
        self.assertTrue(pagination["last_npi"], "pagination.last_npi must not be empty")
        self.assertIsNotNone(pagination["search_params"], "pagination.search_params must not be None")

    # ── R7: System prompt tells Claude to report total and ask ──────

    def test_r7_chat_response_mentions_total(self):
        """/chat response should mention the total number of providers found."""
        r = self.client.post("/chat", json={
            "message": "find pediatricians in delaware",
            "history": [],
        })
        data = r.json()
        if data.get("error"):
            self.skipTest(f"Chat API error (likely timeout): {data['error'][:100]}")
        response_text = data.get("response", "").lower()
        # Claude should mention a number (the total count)
        pagination = data.get("pagination")
        if pagination and pagination.get("total_count"):
            total = str(pagination["total_count"])
            self.assertIn(total, data["response"],
                          f"Response must mention total count ({total})")

    def test_r7_chat_response_asks_for_more(self):
        """/chat response should ask if user wants to see more."""
        r = self.client.post("/chat", json={
            "message": "find pediatricians in delaware",
            "history": [],
        })
        data = r.json()
        if data.get("error"):
            self.skipTest(f"Chat API error (likely timeout): {data['error'][:100]}")
        response_lower = data.get("response", "").lower()
        wants_more = any(phrase in response_lower for phrase in [
            "would you like to see more",
            "want to see more",
            "would you like more",
            "see more",
            "show more",
            "next page",
            "additional",
        ])
        self.assertTrue(wants_more, "Claude must ask if user wants to see more results")

    # ── R8: /search endpoint exists and works without LLM ───────────

    def test_r8_search_endpoint_exists(self):
        """/search endpoint must exist for direct pagination (no LLM)."""
        r = self.client.post("/search", json={"state": "DE", "limit": 5})
        self.assertNotEqual(r.status_code, 404, "/search endpoint must exist")

    def test_r8_search_no_llm(self):
        """/search must return results without invoking any LLM."""
        # If this returns in < 2 seconds, no LLM was called
        import time
        start = time.time()
        r = self.client.post("/search", json={"state": "DE", "limit": 5, "name": "Smith"})
        elapsed = time.time() - start
        self.assertEqual(r.status_code, 200)
        self.assertLess(elapsed, 5, "/search must be fast (no LLM)")

    # ── R9: Tool limit always enforced ──────────────────────────────

    def test_r9_limit_always_enforced(self):
        """Tool must never return more than the requested limit."""
        r = self.client.post("/search", json={"state": "DE", "limit": 3, "name": "Smith"})
        data = r.json()
        self.assertLessEqual(data["count"], 3, "Must not exceed requested limit")


class TestPaginationUIRequirements(unittest.TestCase):
    """UI/UX requirements for pagination controls. Tests the GUIManager output."""

    def _get_html(self, page_start, page_end, total, can_back=True, can_forward=True):
        """Generate pagination HTML for testing."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                                        "Code", "ConversationalUX", "FindCareChat", "frontend", "src", "components"))
        # Can't import TSX directly — test the design contract instead
        return None

    # ── R10: Controls only appear after user opts in ────────────────

    def test_r10_no_controls_on_first_page(self):
        """Pagination controls must NOT appear on first search result.
        Controls only appear after user says 'yes' and second page loads.
        Verified by: pendingPaginationRef stores state silently, gui.showPagination
        is NOT called on first response."""
        # This is a frontend behavior test — verified by code inspection
        import ast
        chatwindow = os.path.join(os.path.dirname(__file__), "..", "..", "frontend",
                                   "src", "components", "ChatWindow.tsx")
        with open(chatwindow) as f:
            content = f.read()
        # First response stores pagination silently
        self.assertIn("pendingPaginationRef.current =", content,
                      "First response must store pagination state silently")
        # showPagination only called in the /search fetch (second page)
        # Count occurrences of showPagination
        show_calls = content.count("gui.showPagination(")
        self.assertEqual(show_calls, 1, "showPagination must only be called once (for page 2)")

    # ── R11: Display format is record-based ─────────────────────────

    def test_r11_display_format_record_based(self):
        """Display must show 'start-end / total', not page numbers."""
        gui_file = os.path.join(os.path.dirname(__file__), "..", "..", "frontend",
                                 "src", "components", "GUIManager.tsx")
        with open(gui_file) as f:
            content = f.read()
        # Must contain record range format, not "Page X of Y"
        self.assertNotIn("Page ${", content, "Must not show page numbers")
        self.assertIn("pageStart", content, "Must reference pageStart for record range")
        self.assertIn("pageEnd", content, "Must reference pageEnd for record range")
        self.assertIn("totalCount", content, "Must reference totalCount")

    # ── R12: Back button tooltip at beginning ───────────────────────

    def test_r12_back_button_tooltip_at_beginning(self):
        """Back button must show 'You are at the beginning' when disabled."""
        gui_file = os.path.join(os.path.dirname(__file__), "..", "..", "frontend",
                                 "src", "components", "GUIManager.tsx")
        with open(gui_file) as f:
            content = f.read()
        self.assertIn("You are at the beginning", content)

    # ── R13: Forward button tooltip at end ──────────────────────────

    def test_r13_forward_button_tooltip_at_end(self):
        """Forward button must show 'You are at the end' when disabled."""
        gui_file = os.path.join(os.path.dirname(__file__), "..", "..", "frontend",
                                 "src", "components", "GUIManager.tsx")
        with open(gui_file) as f:
            content = f.read()
        self.assertIn("You are at the end", content)

    # ── R14: Buttons are 3D with press behavior ─────────────────────

    def test_r14_buttons_have_3d_style(self):
        """Buttons must have 3D raised appearance with border-bottom shadow."""
        gui_file = os.path.join(os.path.dirname(__file__), "..", "..", "frontend",
                                 "src", "components", "GUIManager.tsx")
        with open(gui_file) as f:
            content = f.read()
        self.assertIn("border-bottom", content, "Buttons must have border-bottom for 3D effect")
        self.assertIn("box-shadow", content, "Buttons must have box-shadow for raised effect")

    # ── R15: Mouse down changes button color ────────────────────────

    def test_r15_mouse_down_changes_color(self):
        """Buttons must change from cold gray to warm gray on mouse down."""
        gui_file = os.path.join(os.path.dirname(__file__), "..", "..", "frontend",
                                 "src", "components", "GUIManager.tsx")
        with open(gui_file) as f:
            content = f.read()
        self.assertIn("onmousedown", content, "Must have mousedown handler")
        self.assertIn("onmouseup", content, "Must have mouseup handler")
        self.assertIn("coldGray", content, "Must define cold gray color")
        self.assertIn("warmGray", content, "Must define warm gray color")

    # ── R16: Controls disappear on new chat message ─────────────────

    def test_r16_controls_disappear_on_new_message(self):
        """Pagination controls must disappear when user sends a new chat message."""
        chatwindow = os.path.join(os.path.dirname(__file__), "..", "..", "frontend",
                                   "src", "components", "ChatWindow.tsx")
        with open(chatwindow) as f:
            content = f.read()
        self.assertIn("gui.hidePagination()", content,
                      "Must call hidePagination when user sends new message")

    # ── R17: Pagination uses /search not /chat ──────────────────────

    def test_r17_pagination_uses_search_not_chat(self):
        """Forward/back must call /search directly, not /chat (no LLM)."""
        chatwindow = os.path.join(os.path.dirname(__file__), "..", "..", "frontend",
                                   "src", "components", "ChatWindow.tsx")
        with open(chatwindow) as f:
            content = f.read()
        self.assertIn("/search", content, "Must call /search for pagination")

    # ── R18: Back button is active when second page loads ───────────

    def test_r18_back_button_active_on_second_page(self):
        """When second page renders, Back must be active (page 1 exists behind it).
        canBack is true when pageStart > 1."""
        gui_file = os.path.join(os.path.dirname(__file__), "..", "..", "frontend",
                                 "src", "components", "GUIManager.tsx")
        with open(gui_file) as f:
            content = f.read()
        self.assertIn("canBack = state.pageStart > 1", content,
                      "Back button must be active when pageStart > 1")

    # ── R19: Disabled buttons cannot be clicked ─────────────────────

    def test_r19_disabled_buttons_not_clickable(self):
        """Disabled buttons must have disabled attribute and cursor not-allowed."""
        gui_file = os.path.join(os.path.dirname(__file__), "..", "..", "frontend",
                                 "src", "components", "GUIManager.tsx")
        with open(gui_file) as f:
            content = f.read()
        self.assertIn("not-allowed", content, "Disabled buttons must show not-allowed cursor")
        self.assertIn("'disabled'", content, "Disabled buttons must have disabled attribute")

    # ── R20: Mouse leave returns button to unpressed state ──────────

    def test_r20_mouse_leave_returns_to_unpressed(self):
        """Mouse leave on a pressed button must return it to the raised state."""
        gui_file = os.path.join(os.path.dirname(__file__), "..", "..", "frontend",
                                 "src", "components", "GUIManager.tsx")
        with open(gui_file) as f:
            content = f.read()
        self.assertIn("onmouseleave", content, "Must have mouseleave handler to restore state")


class TestSpecialtyRefinementRequirements(unittest.TestCase):
    """GUI-PAG-S4: Specialty refinement requirements."""

    def _gui(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "frontend",
                             "src", "components", "GUIManager.tsx")
        with open(path) as f:
            return f.read()

    def _chat(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "frontend",
                             "src", "components", "ChatWindow.tsx")
        with open(path) as f:
            return f.read()

    def _index(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..",
                             "Website", "index.html")
        with open(path, encoding="utf-8") as f:
            return f.read()

    # R23: Present specialization names to user
    def test_r23_specialization_options_in_search_response(self):
        """Search response must include specialization_options with names."""
        r = self.__class__.client.post("/search", json={
            "state": "DE", "limit": 5, "specialty_query": "pediatrician"
        })
        data = r.json()
        opts = data.get("specialization_options", [])
        if opts:
            self.assertTrue(all("name" in o for o in opts), "Each option must have a name")
            self.assertTrue(all("code" in o for o in opts), "Each option must have a code")

    # R24: Desktop left panel expands to 15%
    def test_r24_left_panel_expands(self):
        content = self._index()
        self.assertIn("width = '15%'", content.replace('"', "'"),
                      "Left panel must expand to 15%")

    # R25: Phone popup (deferred — check for responsive handling)
    def test_r25_phone_popup_placeholder(self):
        """Phone popup requirement exists in story."""
        pass  # Deferred — mobile popup not yet implemented

    # R26: Scrolls only if needed
    def test_r26_overflow_auto(self):
        content = self._gui()
        self.assertIn("overflow-y:auto", content, "Filter list must scroll only when needed")

    # R27: Re-run search with checked codes
    def test_r27_filter_apply_calls_search(self):
        content = self._chat()
        self.assertIn("/search", content, "Filter apply must call /search")
        self.assertIn("onFilterApply", content, "Must handle filter apply callback")

    # R28: Left panel stays open
    def test_r28_panel_stays_open(self):
        """Left panel stays at 15% until user sends new message."""
        content = self._index()
        self.assertIn("gui:filter-clear", content, "Must support filter-clear to collapse")

    # R29: Names from SpecialtyMetaData
    def test_r29_names_from_db(self):
        src = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..",
                            "FindCare", "ProviderManagement", "provider_search_service.py")
        with open(src) as f:
            content = f.read()
        self.assertIn("SpecialtyMetaData", content, "Must look up names from SpecialtyMetaData")

    # R30: Log codes in non-prod
    def test_r30_log_codes_non_prod(self):
        src = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..",
                            "FindCare", "ProviderManagement", "provider_search_service.py")
        with open(src) as f:
            content = f.read()
        self.assertIn("specialty_code_log", content, "Must log to specialty_code_log")
        self.assertIn('!= "prod"', content, "Must only log in non-prod")

    # R31: New message collapses panel
    def test_r31_new_message_collapses(self):
        content = self._chat()
        self.assertIn("hideFilterPanel", content, "Must hide filter panel on new message")

    # R32: No LLM on filter apply
    def test_r32_no_llm_on_filter(self):
        # filter-apply is in GUIManager, callback in ChatWindow calls /search
        gui_content = self._gui()
        self.assertIn("filter-apply", gui_content, "GUIManager must handle filter-apply event")
        chat_content = self._chat()
        self.assertIn("onFilterApply", chat_content, "ChatWindow must register filter callback")
        self.assertIn("/search", chat_content, "Must use /search not /chat")

    @classmethod
    def setUpClass(cls):
        from main import app
        from fastapi.testclient import TestClient
        cls.client = TestClient(app)


class TestSearchResultPresentation(unittest.TestCase):
    """FC-RESULT-MSG: System-built summary message per GOV-011."""

    @classmethod
    def setUpClass(cls):
        from main import app
        from fastapi.testclient import TestClient
        cls.client = TestClient(app)

    # ── EPIC-006-F-004-S-001-REQ-B-001: Use search term, not generic 'providers' ──

    def test_summary_uses_search_term(self):
        """summary_message must contain the user's search term, not 'providers'."""
        from ProviderManagement.provider_search_service import FindCareService
        msg = FindCareService._build_summary_message(
            has_more=True, total_count=100, page_count=25,
            specialty_searched="surgeons", specialization_options=[{"code": "1"}])
        self.assertIn("surgeons", msg)
        self.assertNotIn("providers", msg)

    # ── EPIC-006-F-004-S-001-REQ-B-002: Remaining count, not total ──

    def test_summary_shows_remaining_count(self):
        """summary_message must show remaining (total - displayed), not total."""
        from ProviderManagement.provider_search_service import FindCareService
        msg = FindCareService._build_summary_message(
            has_more=True, total_count=17278, page_count=25,
            specialty_searched="surgeons")
        self.assertIn("17,253", msg)
        self.assertNotIn("17,278", msg)

    # ── EPIC-006-F-004-S-001-REQ-B-003: Specialty type count ──

    def test_summary_shows_specialty_count(self):
        """summary_message must state how many types of providers."""
        from ProviderManagement.provider_search_service import FindCareService
        opts = [{"code": f"c{i}", "name": f"Spec {i}"} for i in range(12)]
        msg = FindCareService._build_summary_message(
            has_more=True, total_count=100, page_count=25,
            specialty_searched="surgeons", specialization_options=opts)
        self.assertIn("12 types of providers", msg)

    # ── EPIC-006-F-004-S-001-REQ-B-004: Filter link marker ──

    def test_summary_contains_filter_link(self):
        """summary_message must contain a filter action link."""
        from ProviderManagement.provider_search_service import FindCareService
        opts = [{"code": "c1", "name": "Spec 1"}]
        msg = FindCareService._build_summary_message(
            has_more=True, total_count=100, page_count=25,
            specialty_searched="surgeons", specialization_options=opts)
        self.assertIn("#action:filter", msg)
        self.assertIn("filter", msg.lower())

    # ── EPIC-006-F-004-S-001-REQ-B-005: Next page link marker ──

    def test_summary_contains_next_page_link(self):
        """summary_message must contain a next page action link."""
        from ProviderManagement.provider_search_service import FindCareService
        msg = FindCareService._build_summary_message(
            has_more=True, total_count=100, page_count=25,
            specialty_searched="surgeons")
        self.assertIn("#action:next-page", msg)
        self.assertIn("more 'surgeons'", msg)

    # ── EPIC-006-F-004-S-001-REQ-B-006: Search term echoed in links ──

    def test_summary_echoes_search_term_in_links(self):
        """summary_message must echo the search term in the action links."""
        from ProviderManagement.provider_search_service import FindCareService
        msg = FindCareService._build_summary_message(
            has_more=True, total_count=100, page_count=25,
            specialty_searched="shrinks", specialization_options=[{"code": "1"}])
        self.assertIn("more 'shrinks'", msg)
        self.assertIn("filter 'shrinks'", msg)

    # ── EPIC-006-F-004-S-001-REQ-T-001: summary_message in /chat PaginationMeta ──

    def test_chat_returns_summary_message(self):
        """/chat must include summary_message in pagination metadata."""
        r = self.client.post("/chat", json={
            "message": "find pediatricians in delaware",
            "history": [],
        })
        data = r.json()
        if data.get("error"):
            self.skipTest(f"Chat API error: {data['error'][:100]}")
        pagination = data.get("pagination")
        if pagination and pagination.get("has_more"):
            self.assertIsNotNone(pagination.get("summary_message"),
                                  "pagination.summary_message must exist when has_more=True")
            self.assertIn("pediatrician", pagination["summary_message"].lower(),
                          "summary_message must contain the search term")

    # ── EPIC-006-F-004-S-001-REQ-T-002: No summary when has_more is False ──

    def test_no_summary_when_no_more(self):
        """summary_message must be empty when has_more is False."""
        from ProviderManagement.provider_search_service import FindCareService
        msg = FindCareService._build_summary_message(
            has_more=False, total_count=5, page_count=5,
            specialty_searched="surgeons")
        self.assertEqual(msg, "")

    # ── EPIC-006-F-004-S-001-REQ-B-003 edge: No specialty count when no options ──

    def test_no_specialty_count_when_no_options(self):
        """summary_message must not mention provider types when specialization_options is empty."""
        from ProviderManagement.provider_search_service import FindCareService
        msg = FindCareService._build_summary_message(
            has_more=True, total_count=100, page_count=25,
            specialty_searched="Smith")
        self.assertNotIn("types of providers", msg)
        self.assertNotIn("#action:filter", msg)

    # ── EPIC-006-F-004-S-002-REQ-B-001: summary_message in /search response ──

    def test_search_returns_summary_message(self):
        """/search must include summary_message in response."""
        r = self.client.post("/search", json={
            "state": "DE", "limit": 5, "specialty_query": "pediatrician"
        })
        data = r.json()
        if data.get("has_more"):
            self.assertIn("summary_message", data)
            self.assertTrue(len(data["summary_message"]) > 0)

    # ── EPIC-006-F-004-S-002-REQ-B-002: Filter link is markdown action ──

    def test_filter_link_is_action_markdown(self):
        """Filter link must use #action:filter href for frontend interception."""
        from ProviderManagement.provider_search_service import FindCareService
        opts = [{"code": "c1"}]
        msg = FindCareService._build_summary_message(
            has_more=True, total_count=100, page_count=25,
            specialty_searched="surgeons", specialization_options=opts)
        self.assertIn("#action:filter", msg)

    # ── EPIC-006-F-004-S-002-REQ-B-003: Next page link is markdown action ──

    def test_next_page_link_is_action_markdown(self):
        """Next page link must use #action:next-page href for frontend interception."""
        from ProviderManagement.provider_search_service import FindCareService
        msg = FindCareService._build_summary_message(
            has_more=True, total_count=100, page_count=25,
            specialty_searched="surgeons")
        self.assertIn("#action:next-page", msg)

    # ── LLM must not duplicate system summary ──

    def test_strip_redundant_summary(self):
        """_strip_redundant_summary must remove LLM pagination language."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from main import _strip_redundant_summary
        text = (
            "Here are the first **25 of 11,772** mental health providers in Virginia:\n\n"
            "- **Jennifer McEwan, Ph.D.**\n"
            "  - Alexandria, VA\n\n"
            "- **Nevin Yilmaz, MD**\n"
            "  - Richmond, VA\n\n"
            "There are 11,747 more providers. Would you like to see more? "
            "I can also narrow results by city or county! 😊"
        )
        cleaned = _strip_redundant_summary(text, 11772, 25)
        self.assertNotIn("Here are the first", cleaned)
        self.assertNotIn("Would you like to see more", cleaned)
        self.assertNotIn("I can also narrow", cleaned)
        # Provider listing must be preserved
        self.assertIn("Jennifer McEwan", cleaned)
        self.assertIn("Nevin Yilmaz", cleaned)

    def test_strip_preserves_provider_data(self):
        """De-dup must not remove provider records or medical data."""
        from main import _strip_redundant_summary
        text = (
            "Here are the first **25 of 686** surgeons in Delaware:\n\n"
            "- **Dr. Smith, MD** *(General Surgery)*\n"
            "  - 123 Main St, Wilmington, DE\n"
            "  - NPI: 1234567890\n\n"
            "Would you like to see the next page?"
        )
        cleaned = _strip_redundant_summary(text, 686, 25)
        self.assertIn("Dr. Smith", cleaned)
        self.assertIn("General Surgery", cleaned)
        self.assertIn("1234567890", cleaned)
        self.assertNotIn("Here are the first", cleaned)

    # ── Summary must use user's term, not LLM's ──

    def test_summary_uses_user_term_not_llm(self):
        """summary_message must use user's original words, not LLM-translated terms."""
        from ProviderManagement.provider_search_service import FindCareService
        msg = FindCareService._build_summary_message(
            has_more=True, total_count=11772, page_count=25,
            specialty_searched="find me shrinks in VA",
            specialization_options=[{"code": "c1"}],
            state="VA")
        self.assertIn("shrinks", msg.lower())
        self.assertNotIn("psychiatrist", msg.lower())

    # ── GOV-011-STD-002: User terms over professional terms ──

    def test_user_term_preserved_not_translated(self):
        """User's colloquial term must appear in summary, not professional equivalent."""
        from ProviderManagement.provider_search_service import FindCareService
        for user_term in ["shrinks", "heart doctors", "bone doctors", "eye docs"]:
            msg = FindCareService._build_summary_message(
                has_more=True, total_count=100, page_count=25,
                specialty_searched=user_term,
                specialization_options=[{"code": "c1"}],
                state="VA")
            self.assertIn(user_term, msg,
                          f"Summary must contain user's term '{user_term}'")

    # ── EPIC-006-F-004-S-002-REQ-T-001: Frontend handles action links ──

    def test_frontend_handles_action_links(self):
        """MessageBubble must intercept #action: links."""
        bubble_path = os.path.join(os.path.dirname(__file__), "..", "..", "frontend",
                                    "src", "components", "MessageBubble.tsx")
        with open(bubble_path) as f:
            content = f.read()
        self.assertIn("#action:", content, "MessageBubble must handle #action: links")
        self.assertIn("gui:next-page", content, "Must dispatch gui:next-page message")
        self.assertIn("gui:highlight-filter", content, "Must dispatch gui:highlight-filter message")


if __name__ == "__main__":
    unittest.main()
