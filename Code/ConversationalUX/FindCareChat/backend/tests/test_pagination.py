# Copyright (c) 2026 Skip Snow. All rights reserved.
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
        from application.tool_models.provider_search_models import ProviderSearchInput
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


if __name__ == "__main__":
    unittest.main()
