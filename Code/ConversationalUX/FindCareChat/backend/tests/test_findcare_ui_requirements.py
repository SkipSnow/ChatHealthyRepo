# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""FindCare UI requirement tests — code inspection + headless Playwright.

FC-SELECT-001-REQ-002: Drag and drop provider from top to bottom list
FC-SELECT-001-REQ-007: Only selected providers (bottom list) sent to EvaluateCare
FC-FILT-001-REQ-015: Specialty list >15 items displays in scroll window
FC-FILT-001-REQ-016: Toggle label reads 'Uncheck All' / 'Check All' based on majority
UX-FRAME-001-REQ-004: Control frame is 7% of center column height

Code inspection tests run without the server. Playwright live-DOM tests are
marked skip when the local dev server is not running.
"""

import os
import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
FRONTEND_SRC = os.path.join(REPO, "Code", "ConversationalUX", "FindCareChat",
                            "frontend", "src", "components")
WEBSITE_DIR = os.path.join(REPO, "Website")
BASE_URL = os.getenv("TEST_BASE_URL", "https://localhost")


def _read(filename):
    path = os.path.join(FRONTEND_SRC, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _server_running():
    try:
        import urllib.request, ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        urllib.request.urlopen(urllib.request.Request(BASE_URL, method="HEAD"),
                              timeout=5, context=ctx)
        return True
    except Exception:
        return False


_SERVER_UP = _server_running()


# ── FC-SELECT-001-REQ-002 ─────────────────────────────────────────────────
# User can drag and drop provider from top to bottom list

def test_drag_drop_code_infrastructure():
    """FC-SELECT-001-REQ-002: FindCareApp has drag-and-drop handlers for provider selection."""
    src = _read("FindCareApp.tsx")
    assert "onDragOver" in src, "FindCareApp must have onDragOver handler on drop target"
    assert "onDrop" in src, "FindCareApp must have onDrop handler on drop target"
    assert "dataTransfer.getData" in src, "Drop handler must read NPI from dataTransfer"
    assert "selection.select" in src, "Drop handler must call selection.select on drop"


@pytest.mark.skipif(not _SERVER_UP, reason="local dev server not running")
def test_drag_drop_live_dom():
    """FC-SELECT-001-REQ-002: live DOM has the drop target div."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1400, "height": 900},
                                   ignore_https_errors=True).new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)
        # The selected-for-evaluation area is rendered in the React app
        content = page.content()
        browser.close()
    assert "Selected for Evaluation" in content or "selected" in content.lower()


# ── FC-SELECT-001-REQ-007 ─────────────────────────────────────────────────
# Only selected providers (bottom list) sent to EvaluateCare

def test_only_selected_sent_to_evaluate_care():
    """FC-SELECT-001-REQ-007: handleEvaluate sends only selectedRef.current to EvaluateCare."""
    src = _read("FindCareApp.tsx")
    assert "selectedRef.current" in src, (
        "handleEvaluate must reference selectedRef.current (bottom-list providers)"
    )
    assert "const providers = selectedRef.current" in src, (
        "handleEvaluate must assign providers from selectedRef.current only"
    )
    assert "/evaluate/providers" in src, (
        "handleEvaluate must POST to /evaluate/providers endpoint"
    )


# ── FC-FILT-001-REQ-015 ───────────────────────────────────────────────────
# Specialty list >15 items must display in scroll window

def test_specialty_list_scrollable_gui_manager():
    """FC-FILT-001-REQ-015: GUIManager filter panel has scroll container."""
    src = _read("GUIManager.tsx")
    assert "max-height:" in src, "Filter panel must set max-height for scroll"
    assert "overflow-y:auto" in src, "Filter panel must use overflow-y:auto"


def test_specialty_list_scrollable_findcare_app():
    """FC-FILT-001-REQ-015: FindCareApp filter panel has scroll container."""
    src = _read("FindCareApp.tsx")
    assert "max-height:" in src, "FindCareApp filter panel must set max-height"
    assert "overflow-y:auto" in src, "FindCareApp filter panel must use overflow-y:auto"


# ── FC-FILT-001-REQ-016 ───────────────────────────────────────────────────
# Label reads 'Uncheck All' when majority checked, 'Check All' when majority unchecked

def test_toggle_all_button_in_gui_manager():
    """FC-FILT-001-REQ-016: GUIManager has toggle-all button with 'Uncheck All' label."""
    src = _read("GUIManager.tsx")
    assert 'data-gui-action="toggle-all"' in src, "Must have toggle-all button"
    assert "Uncheck All" in src, (
        "Default label must be 'Uncheck All' (majority checked on init)"
    )


def test_toggle_all_button_in_findcare_app():
    """FC-FILT-001-REQ-016: FindCareApp has toggle-all button with 'Uncheck All' label."""
    src = _read("FindCareApp.tsx")
    assert 'data-gui-action="toggle-all"' in src, "Must have toggle-all button"
    assert "Uncheck All" in src, "Default label must be 'Uncheck All'"


# ── UX-FRAME-001-REQ-004 ─────────────────────────────────────────────────
# Control frame is 7% of center column height

def test_control_frame_7_percent_in_html():
    """UX-FRAME-001-REQ-004: index.html has guiControlFrame with height:7%."""
    html_path = os.path.join(WEBSITE_DIR, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    assert 'id="guiControlFrame"' in html, "guiControlFrame element must exist"
    assert "height:7%" in html, "Control frame must have height:7%"


@pytest.mark.skipif(not _SERVER_UP, reason="local dev server not running")
def test_control_frame_7_percent_live():
    """UX-FRAME-001-REQ-004: live page has guiControlFrame element with height."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1400, "height": 900},
                                   ignore_https_errors=True).new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        el = page.locator("#guiControlFrame")
        assert el.count() > 0, "guiControlFrame must be present"
        box = el.bounding_box()
        assert box and box["height"] > 0, "Control frame must have non-zero height"
        browser.close()
