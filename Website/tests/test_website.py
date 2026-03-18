"""
Unit tests for Website/index.html — legal panel and navigation behavior.
Run from the repo root:
    pip install pytest-playwright && playwright install chromium
    python -m pytest Website/tests/test_website.py -v
"""
import re

import pytest
from playwright.sync_api import Page, expect


# ── Helpers ──────────────────────────────────────────────────────────────────

PANEL       = "#legalPanel"
PANEL_TITLE = "#legalPanelTitle"
PANEL_FRAME = "#legalPanelFrame"
PANEL_CLOSE = ".legal-panel-close"

OPEN_CLASS = re.compile(r"\bopen\b")


def open_panel(page: Page, label: str):
    """Click the first button matching label (nav takes priority over footer)."""
    page.get_by_role("button", name=label).first.click()
    expect(page.locator(PANEL)).to_have_class(OPEN_CLASS)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def home(page: Page, base_url: str):
    """Navigate to index.html before each test."""
    page.goto(f"{base_url}/index.html")
    return page


# ── Panel open ───────────────────────────────────────────────────────────────

def test_privacy_panel_opens(home: Page):
    """Clicking Privacy Policy opens the panel with correct title and src."""
    open_panel(home, "Privacy Policy")
    expect(home.locator(PANEL_TITLE)).to_have_text("Privacy Policy")
    expect(home.locator(PANEL_FRAME)).to_have_attribute("src", "privacy.html")


def test_terms_panel_opens(home: Page):
    """Clicking Terms of Use opens the panel with correct title and src."""
    open_panel(home, "Terms of Use")
    expect(home.locator(PANEL_TITLE)).to_have_text("Terms of Use")
    expect(home.locator(PANEL_FRAME)).to_have_attribute("src", "terms.html")


def test_panel_footer_privacy_opens(home: Page):
    """Footer Privacy Policy button also opens the panel."""
    home.get_by_role("button", name="Privacy Policy").last.click()
    expect(home.locator(PANEL)).to_have_class(OPEN_CLASS)
    expect(home.locator(PANEL_TITLE)).to_have_text("Privacy Policy")


def test_panel_footer_terms_opens(home: Page):
    """Footer Terms of Use button also opens the panel."""
    home.get_by_role("button", name="Terms of Use").last.click()
    expect(home.locator(PANEL)).to_have_class(OPEN_CLASS)
    expect(home.locator(PANEL_TITLE)).to_have_text("Terms of Use")


# ── Panel close ──────────────────────────────────────────────────────────────

def test_panel_closes_with_x_button(home: Page):
    """Clicking the × button removes the open class."""
    open_panel(home, "Privacy Policy")
    home.locator(PANEL_CLOSE).click()
    expect(home.locator(PANEL)).not_to_have_class(OPEN_CLASS)


def test_panel_closes_with_escape(home: Page):
    """Pressing Escape removes the open class."""
    open_panel(home, "Terms of Use")
    home.keyboard.press("Escape")
    expect(home.locator(PANEL)).not_to_have_class(OPEN_CLASS)


def test_panel_src_cleared_after_close(home: Page):
    """After close animation, iframe src is cleared to stop background loading."""
    open_panel(home, "Privacy Policy")
    home.locator(PANEL_CLOSE).click()
    # src is cleared after a 300ms timeout — wait up to 2s
    expect(home.locator(PANEL_FRAME)).to_have_attribute("src", "", timeout=2000)


def test_panel_can_switch_between_pages(home: Page):
    """Opening Terms after Privacy updates title and src correctly."""
    open_panel(home, "Privacy Policy")
    # Panel is open and covers footer — call JS directly to switch pages
    home.evaluate("openPanel('terms.html', 'Terms of Use')")
    expect(home.locator(PANEL_TITLE)).to_have_text("Terms of Use")
    expect(home.locator(PANEL_FRAME)).to_have_attribute("src", "terms.html")


# ── Coming Soon buttons ──────────────────────────────────────────────────────

def test_contact_button_shows_tip(home: Page):
    """Contact button in footer shows contact info tooltip on click."""
    contact_btn = home.locator(".footer-links button[data-tip]").first
    contact_btn.click()
    expect(contact_btn).to_have_class(re.compile(r"\bshow-tip\b"))


def test_contact_tip_dismissed_on_outside_click(home: Page):
    """Clicking outside the Contact button removes its tooltip."""
    contact_btn = home.locator(".footer-links button[data-tip]").first
    contact_btn.click()
    expect(contact_btn).to_have_class(re.compile(r"\bshow-tip\b"))
    home.locator("header").click()
    expect(contact_btn).not_to_have_class(re.compile(r"\bshow-tip\b"))


def test_about_button_removed(home: Page):
    """About button no longer exists in the footer."""
    expect(home.locator(".footer-links button", has_text="About")).to_have_count(0)


# ── Panel does not block chat iframe ─────────────────────────────────────────

def test_chat_iframe_present(home: Page):
    """Chat iframe is present in the DOM before panel is opened."""
    expect(home.locator("#coreChatFrame")).to_be_attached()


def test_chat_iframe_still_present_while_panel_open(home: Page):
    """Chat iframe remains in DOM while legal panel is open (non-modal)."""
    open_panel(home, "Privacy Policy")
    expect(home.locator("#coreChatFrame")).to_be_attached()


# ── No recursive windows ──────────────────────────────────────────────────────

def test_privacy_page_has_no_panel_links(page: Page, base_url: str):
    """privacy.html has no openPanel() calls — prevents recursive iframe loading."""
    page.goto(f"{base_url}/privacy.html")
    # There should be no elements that call openPanel
    panel_triggers = page.locator("[onclick*='openPanel']")
    expect(panel_triggers).to_have_count(0)


def test_terms_page_has_no_panel_links(page: Page, base_url: str):
    """terms.html has no openPanel() calls — prevents recursive iframe loading."""
    page.goto(f"{base_url}/terms.html")
    panel_triggers = page.locator("[onclick*='openPanel']")
    expect(panel_triggers).to_have_count(0)


def test_architecture_page_has_no_panel_links(page: Page, base_url: str):
    """architecture.html has no openPanel() calls — prevents recursive iframe loading."""
    page.goto(f"{base_url}/architecture.html")
    panel_triggers = page.locator("[onclick*='openPanel']")
    expect(panel_triggers).to_have_count(0)


def test_privacy_close_button_visible_in_panel(home: Page):
    """Privacy page shows close button at the bottom when in the panel."""
    open_panel(home, "Privacy Policy")
    expect(home.frame_locator("#legalPanelFrame").locator(".action-bar-bottom .close-btn")).to_be_visible()


def test_terms_close_button_visible_in_panel(home: Page):
    """Terms page shows close button at the bottom when in the panel."""
    home.evaluate("openPanel('terms.html', 'Terms of Use')")
    expect(home.locator(PANEL)).to_have_class(OPEN_CLASS)
    expect(home.frame_locator("#legalPanelFrame").locator(".action-bar-bottom .close-btn")).to_be_visible()


def test_privacy_bottom_close_button_closes_panel(home: Page):
    """Bottom close button inside privacy panel closes the panel."""
    open_panel(home, "Privacy Policy")
    frame = home.frame_locator("#legalPanelFrame")
    frame.locator(".action-bar-bottom .close-btn").scroll_into_view_if_needed()
    frame.locator(".action-bar-bottom .close-btn").click()
    expect(home.locator(PANEL)).not_to_have_class(OPEN_CLASS)


def test_privacy_print_button_present(home: Page):
    """Privacy page has a print button at the bottom."""
    open_panel(home, "Privacy Policy")
    expect(home.frame_locator("#legalPanelFrame").locator(".action-bar-bottom .print-btn")).to_be_visible()


def test_terms_print_button_present(home: Page):
    """Terms page has a print button at the bottom."""
    home.evaluate("openPanel('terms.html', 'Terms of Use')")
    expect(home.locator(PANEL)).to_have_class(OPEN_CLASS)
    expect(home.frame_locator("#legalPanelFrame").locator(".action-bar-bottom .print-btn")).to_be_visible()


def test_privacy_header_hidden_in_panel(home: Page):
    """Privacy page header is hidden when loaded inside the panel iframe."""
    open_panel(home, "Privacy Policy")
    frame = home.frame_locator("#legalPanelFrame")
    expect(frame.locator("header")).to_be_hidden()


def test_terms_header_hidden_in_panel(home: Page):
    """Terms page header is hidden when loaded inside the panel iframe."""
    open_panel(home, "Terms of Use")
    frame = home.frame_locator("#legalPanelFrame")
    expect(frame.locator("header")).to_be_hidden()


def test_privacy_logo_targets_top(page: Page, base_url: str):
    """Logo on privacy.html uses target=_top to break out of any iframe."""
    page.goto(f"{base_url}/privacy.html")
    logo = page.locator("header .logo")
    expect(logo).to_have_attribute("target", "_top")


def test_terms_logo_targets_top(page: Page, base_url: str):
    """Logo on terms.html uses target=_top to break out of any iframe."""
    page.goto(f"{base_url}/terms.html")
    logo = page.locator("header .logo")
    expect(logo).to_have_attribute("target", "_top")


def test_architecture_logo_targets_top(page: Page, base_url: str):
    """Logo on architecture.html uses target=_top to break out of any iframe."""
    page.goto(f"{base_url}/architecture.html")
    logo = page.locator("header .logo")
    expect(logo).to_have_attribute("target", "_top")


def test_architecture_panel_opens_from_footer(home: Page):
    """Architecture button in footer opens the panel with correct title and src."""
    home.locator(".footer-links button", has_text="Architecture").click()
    expect(home.locator(PANEL)).to_have_class(OPEN_CLASS)
    expect(home.locator(PANEL_TITLE)).to_have_text("Architecture")
    expect(home.locator(PANEL_FRAME)).to_have_attribute("src", "architecture.html")


def test_architecture_close_button_visible_in_panel(home: Page):
    """Architecture page shows close button at the bottom when in the panel."""
    open_panel(home, "Architecture")
    expect(home.frame_locator("#legalPanelFrame").locator(".action-bar-bottom .close-btn")).to_be_visible()


def test_architecture_print_button_present(home: Page):
    """Architecture page has a print button at the bottom."""
    open_panel(home, "Architecture")
    expect(home.frame_locator("#legalPanelFrame").locator(".action-bar-bottom .print-btn")).to_be_visible()


def test_architecture_header_hidden_in_panel(home: Page):
    """Architecture page header is hidden when loaded inside the panel iframe."""
    open_panel(home, "Architecture")
    frame = home.frame_locator("#legalPanelFrame")
    expect(frame.locator("header")).to_be_hidden()


# ── Products & Services panel ─────────────────────────────────────────────────

def test_products_panel_opens_from_nav(home: Page):
    """Products & Services button in nav opens the panel with correct title and src."""
    open_panel(home, "Products & Services")
    expect(home.locator(PANEL_TITLE)).to_have_text("Products & Services")
    expect(home.locator(PANEL_FRAME)).to_have_attribute("src", "products.html")


def test_products_page_has_no_panel_links(page: Page, base_url: str):
    """products.html has no openPanel() calls — prevents recursive iframe loading."""
    page.goto(f"{base_url}/products.html")
    expect(page.locator("[onclick*='openPanel']")).to_have_count(0)


def test_products_logo_targets_top(page: Page, base_url: str):
    """Logo on products.html uses target=_top to break out of any iframe."""
    page.goto(f"{base_url}/products.html")
    expect(page.locator("header .logo")).to_have_attribute("target", "_top")


def test_products_header_hidden_in_panel(home: Page):
    """Products page header is hidden when loaded inside the panel iframe."""
    open_panel(home, "Products & Services")
    expect(home.frame_locator("#legalPanelFrame").locator("header")).to_be_hidden()


def test_products_close_button_visible_in_panel(home: Page):
    """Products page shows close button at the bottom when in the panel."""
    open_panel(home, "Products & Services")
    expect(home.frame_locator("#legalPanelFrame").locator(".action-bar-bottom .close-btn")).to_be_visible()


def test_products_print_button_present(home: Page):
    """Products page has a print button at the bottom."""
    open_panel(home, "Products & Services")
    expect(home.frame_locator("#legalPanelFrame").locator(".action-bar-bottom .print-btn")).to_be_visible()


# ── Panel expand button ───────────────────────────────────────────────────────

def test_panel_expand_button_present(home: Page):
    """Expand button is present in the panel header."""
    expect(home.locator("#panelExpandBtn")).to_be_attached()


def test_panel_expand_increases_width(home: Page):
    """Clicking expand button increases panel width beyond the default 520px."""
    open_panel(home, "Privacy Policy")
    default_width = home.locator("#legalPanel").bounding_box()["width"]
    home.locator("#panelExpandBtn").click()
    expanded_width = home.locator("#legalPanel").bounding_box()["width"]
    assert expanded_width > default_width


def test_panel_expand_toggle_collapses(home: Page):
    """Clicking expand twice returns panel to narrower width."""
    open_panel(home, "Privacy Policy")
    home.locator("#panelExpandBtn").click()
    expanded_width = home.locator("#legalPanel").bounding_box()["width"]
    home.locator("#panelExpandBtn").click()
    collapsed_width = home.locator("#legalPanel").bounding_box()["width"]
    assert collapsed_width < expanded_width


def test_panel_resize_handle_present(home: Page):
    """Resize drag handle is present on the panel left edge."""
    expect(home.locator("#panelResizeHandle")).to_be_attached()


# ── Mobile responsive ─────────────────────────────────────────────────────────

MOBILE = {"width": 390, "height": 844}   # iPhone 14 viewport
TABLET = {"width": 768, "height": 1024}


@pytest.fixture()
def mobile(page: Page, base_url: str):
    page.set_viewport_size(MOBILE)
    page.goto(f"{base_url}/index.html")
    return page


@pytest.fixture()
def mobile_privacy(page: Page, base_url: str):
    page.set_viewport_size(MOBILE)
    page.goto(f"{base_url}/privacy.html")
    return page


def test_mobile_hamburger_visible(mobile: Page):
    """Hamburger menu button is visible on mobile."""
    expect(mobile.locator("#menuToggle")).to_be_visible()


def test_mobile_header_nav_hidden(mobile: Page):
    """Desktop nav is hidden on mobile."""
    expect(mobile.locator(".header-nav")).to_be_hidden()


def test_mobile_hamburger_opens_drawer(mobile: Page):
    """Tapping hamburger opens the mobile nav drawer."""
    mobile.locator("#menuToggle").click()
    expect(mobile.locator("#mobileNav")).to_have_class(re.compile(r"\bopen\b"))


def test_mobile_panel_full_width(mobile: Page):
    """Legal panel fills the full viewport width on mobile."""
    open_panel(mobile, "Privacy Policy")
    panel = mobile.locator("#legalPanel")
    box = panel.bounding_box()
    assert box is not None
    assert box["width"] == pytest.approx(MOBILE["width"], abs=2)


def test_mobile_privacy_page_readable(mobile_privacy: Page):
    """Privacy page h1 and body text are visible on mobile."""
    expect(mobile_privacy.locator(".page-header h1")).to_be_visible()
    expect(mobile_privacy.locator(".policy-body")).to_be_visible()


def test_mobile_table_scrollable(mobile_privacy: Page):
    """Vendor tables are wrapped in a scrollable container on mobile."""
    wrappers = mobile_privacy.locator(".table-wrap")
    count = wrappers.count()
    assert count > 0, "Expected at least one .table-wrap element"
    for i in range(count):
        expect(wrappers.nth(i)).to_be_visible()


def test_tablet_header_nav_visible(page: Page, base_url: str):
    """Desktop nav remains visible at tablet width (768px)."""
    page.set_viewport_size(TABLET)
    page.goto(f"{base_url}/index.html")
    expect(page.locator(".header-nav")).to_be_visible()
