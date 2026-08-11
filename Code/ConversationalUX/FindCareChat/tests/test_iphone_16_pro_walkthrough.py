"""iPhone 16 Pro full-flow profile walkthrough.

Uses Playwright's iPhone 15 Pro device descriptor (closest available) with
the 16 Pro CSS viewport override (402x874) and full mobile emulation:
real iOS Safari UA, device_scale_factor=3, is_mobile=True, has_touch=True.

The test does NOT assert specific spec rules (those live in
test_iphone_hig_compliance.py). It walks the full flow and captures one
screenshot per transition so we can SEE what breaks on phone. Use it
as a regression observability tool.

Output: _oneshots/test_output/iphone_16_pro/<NN>_<name>.png

Run against local (default):
    python -m pytest Code/ConversationalUX/FindCareChat/tests/test_iphone_16_pro_walkthrough.py -v -s

Run against dev:
    BASE_URL=https://dev.chathealthy.ai/ python -m pytest ... -v -s
"""
from __future__ import annotations
import os
from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "ChatHealthyLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()


BASE_URL = os.environ.get("BASE_URL", "https://localhost/")
SEARCH_QUERY = "find me a bone doc in DE"
HEADED = os.environ.get("HEADED") in ("1", "true", "TRUE", "yes")
SLOW_MO = 350 if HEADED else 0

# iPhone 16 Pro CSS viewport. Closest Playwright device descriptor is
# iPhone 15 Pro (393x852); we override viewport so the test target is
# Skip's actual UAT device.
IPHONE_16_PRO_VIEWPORT = {"width": 402, "height": 874}

SHOTS_DIR = Path(__file__).resolve().parents[4] / "_oneshots" / "test_output" / "iphone_16_pro"
SHOTS_DIR.mkdir(parents=True, exist_ok=True)


def _shot(page, n: int, name: str):
    path = SHOTS_DIR / f"{n:02d}_{name}.png"
    page.screenshot(path=str(path), full_page=True)
    _CH_LOG.info(f"  saved {path.name}")


def test_iphone_16_pro_full_flow():
    with sync_playwright() as p:
        ios_profile = dict(p.devices["iPhone 15 Pro"])
        ios_profile["viewport"] = IPHONE_16_PRO_VIEWPORT
        ios_profile["screen"] = IPHONE_16_PRO_VIEWPORT
        # Bump the UA's iOS version reference to 17/18 era for realism.
        ios_profile["user_agent"] = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
        )
        browser = p.chromium.launch(headless=not HEADED, slow_mo=SLOW_MO)
        ctx = browser.new_context(ignore_https_errors=True, **ios_profile)
        page = ctx.new_page()
        _CH_LOG.info(f"\nWalkthrough target: {BASE_URL}")
        _CH_LOG.info(f"Viewport: {IPHONE_16_PRO_VIEWPORT}")
        _CH_LOG.info(f"Output: {SHOTS_DIR}")

        # ── 01: landing page (welcome) ───────────────────────────
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(1200)
        _shot(page, 1, "landing_welcome")

        chat = page.locator("#coreChatFrame").content_frame
        assert chat is not None, "chat iframe missing on landing"

        # ── 02: type a query ─────────────────────────────────────
        chat_input = chat.locator("input[placeholder*='Type a message'], textarea").first
        chat_input.click(); page.wait_for_timeout(300)
        _shot(page, 2, "input_focused")
        chat_input.fill(SEARCH_QUERY)
        page.wait_for_timeout(300)
        _shot(page, 3, "query_typed")

        # ── 03: send → search in progress ────────────────────────
        chat_input.press("Enter")
        page.wait_for_timeout(2500)
        _shot(page, 4, "search_in_progress")

        # ── 04: providers visible ────────────────────────────────
        try:
            chat.locator("[data-testid='available-providers']").wait_for(timeout=45000)
        except Exception:
            _shot(page, 5, "providers_FAILED_TO_LOAD")
            _CH_LOG.info("FAIL: providers did not load")
            browser.close()
            raise
        page.wait_for_timeout(800)
        _shot(page, 5, "providers_visible")

        # ── 05: filter overlay (mobile) ──────────────────────────
        # The Filter FAB should be visible per body.has-filter-results CSS
        # gate. Click it to bring the filter overlay up.
        fab = page.locator("#mobileFilterReopen")
        fab_present = fab.count() > 0
        fab_visible = fab_present and fab.is_visible()
        _CH_LOG.info(f"  Filter FAB: present={fab_present} visible={fab_visible}")
        if fab_visible:
            fab.click(); page.wait_for_timeout(800)
            _shot(page, 6, "filter_overlay_opened_via_FAB")
        else:
            _shot(page, 6, "no_filter_FAB_FAIL")

        # ── 06: toggle a row + Apply Filter ──────────────────────
        filt = next((f for f in page.frames if "mode=filter" in (f.url or "")), None)
        if filt is None:
            _shot(page, 7, "no_filter_iframe_FAIL")
            _CH_LOG.info("FAIL: filter sub-iframe not present")
        else:
            try:
                filt.locator("[data-testid='specialty-filter']").wait_for(timeout=8000)
                filt.locator(".specialty-filter__row").first.click()
                page.wait_for_timeout(400)
                _shot(page, 7, "filter_row_toggled")
                apply_btn = filt.locator("[data-testid='apply-filter-button']")
                apply_btn.click(); page.wait_for_timeout(3500)
                _shot(page, 8, "after_apply_filter_back_to_providers")
            except Exception as e:
                _shot(page, 7, "filter_interact_FAIL")
                _CH_LOG.info(f"FAIL filter interaction: {e}")

        # ── 07: select a provider ────────────────────────────────
        try:
            arrows = chat.locator("button:has-text('↓')")
            if arrows.count() > 0:
                arrows.first.click()
                page.wait_for_timeout(800)
                _shot(page, 9, "provider_selected")
            else:
                _shot(page, 9, "no_select_arrow_FAIL")
        except Exception as e:
            _shot(page, 9, "select_FAIL")
            _CH_LOG.info(f"FAIL select: {e}")

        # ── 08: scroll the available providers list ──────────────
        try:
            chat.locator("[data-testid='available-providers']").evaluate(
                "el => el.scrollBy(0, 400)"
            )
            page.wait_for_timeout(400)
            _shot(page, 10, "providers_scrolled")
        except Exception as e:
            _CH_LOG.info(f"scroll error: {e}")

        # ── 09: scroll page-level (parent) ───────────────────────
        page.evaluate("window.scrollTo(0, 200)")
        page.wait_for_timeout(300)
        _shot(page, 11, "page_scrolled")

        # ── 10: tap Evaluate button if visible ───────────────────
        eval_btn = page.locator("#guiEvalBtn")
        if eval_btn.count() > 0 and eval_btn.is_visible():
            eval_btn.click()
            page.wait_for_timeout(2000)
            _shot(page, 12, "evaluate_clicked")
        else:
            _shot(page, 12, "no_eval_button_FAIL")

        browser.close()
        _CH_LOG.info(f"\nWalkthrough complete. Screenshots: {SHOTS_DIR}")
