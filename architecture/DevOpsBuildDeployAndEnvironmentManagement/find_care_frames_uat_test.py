# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# find_care_frames_uat_test.py — UAT for every defect and feature worked
# 2026-08-25/26, asserted against the DOM the user is looking at.
#
# Why: a deploy reported "verification: 8 passed, 0 failed" while clicking
# Apply Filter destroyed the specialty panel. All eight checks were HTTP
# reachability and mTLS handshakes. Every service was up and the screen was
# broken, because nothing asked what the user would see.
#
# Each class below is one defect, named with what went wrong, so a failure
# says which behaviour regressed rather than which selector moved.
#
# DOM facts these rely on (measured, not assumed): the frames are DIV /
# ASIDE / HEADER elements carrying id="frame_*" in the TOP document, not
# iframes. There is exactly one iframe, data-frame="MainWindow"
# id="coreReactFrame", and the React app inside it paints the top-level
# frames by postMessage. The prompt is a single <input> inside
# #frame_UserPromptAndControl.

import os

import pytest
from playwright.sync_api import expect, sync_playwright

BASE_URL = os.getenv("SMOKE_TEST_URL", "https://localhost")
DEFAULT_TIMEOUT = 60_000
LLM_TIMEOUT = 240_000          # a turn runs normalize + embed + vector + filter

SCREENSHOT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "_oneshots", "test_output", "frames_uat",
)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

PROMPT = "#frame_UserPromptAndControl input"
PANEL = "#frame_LeftPanel"
RESULTS = "#frame_MainWindow"
PANEL_BOXES = f"{PANEL} input[type='checkbox']"
PANEL_TICKED = f"{PANEL} input[type='checkbox']:checked"

# Every checkbox in the panel carries pointer-events:none and the click
# target is the element around it -- the row for a specialty, the label for
# a macro. So the boxes are read, never clicked: a test that clicks one is
# reaching for something the user cannot reach, and Playwright says so by
# reporting the row or label intercepting the click.
PANEL_ROWS = f"{PANEL} tr[data-code]"
APPLY = f"{PANEL} [data-testid='apply-filter-button']"


def _shot(page, name):
    page.screenshot(path=os.path.join(SCREENSHOT_DIR, f"{name}.png"), full_page=True)


def _ask(page, text):
    """Type and send, as a person does."""
    page.locator(PROMPT).fill(text)
    page.locator(PROMPT).press("Enter")


def _rows(page):
    """[(code, ticked)] for every specialty row, as the user sees it."""
    return page.evaluate(
        f"() => Array.from(document.querySelectorAll('{PANEL_ROWS}')).map("
        "r => [r.getAttribute('data-code'),"
        " !!(r.querySelector(\"input[type='checkbox']\") || {}).checked])")


def _ticked_codes(page):
    return [code for code, ticked in _rows(page) if ticked]


def _click_row(page, code):
    """Toggle one specialty and wait for the panel to show it toggled.

    The click is a postMessage to the parent, which repaints the frame, so
    the tick does not change on the same turn as the click. Reading it
    immediately reads the state before the repaint.
    """
    was = code in _ticked_codes(page)
    page.locator(f"{PANEL} tr[data-code='{code}']").first.click()
    page.wait_for_function(
        f"() => {{ const r = document.querySelector"
        f"(\"{PANEL} tr[data-code='{code}']\");"
        f" return r && r.querySelector(\"input[type='checkbox']\").checked"
        f" === {str(not was).lower()}; }}",
        timeout=DEFAULT_TIMEOUT)


def _uncheck_all(page):
    """Clear every tick through the panel's own control.

    The button reads "Check All" until every row is ticked and "Uncheck
    All" only then, so clearing a partial selection is two clicks: fill it,
    then empty it.
    """
    toggle = page.locator(f"{PANEL} [data-testid='toggle-all-button']")
    for _ in range(2):
        if not _ticked_codes(page):
            return
        toggle.click()
        page.wait_for_timeout(500)
    assert _ticked_codes(page) == [], "the panel would not clear"


def _apply(page, settle_ms):
    """Apply the current selection.

    Apply Filter is disabled until the selection differs from the one in
    force -- there is nothing to apply otherwise -- so the caller has to
    have changed something first.
    """
    expect(page.locator(APPLY)).to_be_enabled()
    page.locator(APPLY).click()
    page.wait_for_timeout(settle_ms)


def _wait_for_panel(page):
    page.wait_for_function(
        f"() => document.querySelectorAll(\"{PANEL_BOXES}\").length > 0",
        timeout=LLM_TIMEOUT)


# The results header agrees with its number -- "1 provider found", "151
# providers found" -- so a test that matches only the plural reads a single
# result as no result at all.
FOUND = ("providers found", "provider found")
_FOUND_JS = " || ".join(f"t.includes('{token}')" for token in FOUND)


def _wait_for_results(page):
    page.wait_for_function(
        "() => { const t = (document.querySelector('#frame_MainWindow')"
        f" || {{}}).textContent || ''; return {_FOUND_JS}; }}",
        timeout=LLM_TIMEOUT)


def _new_search(page, text):
    """Ask a fresh question and wait for the screen to actually turn over.

    Waiting only for "provider found" is satisfied by the PREVIOUS result,
    which is still on screen when the question is sent -- so the wait
    returns immediately and the assertions read the old page. A new query
    emits intent_classified, which blanks the content frames, so the frame
    changing is the signal that the new turn has started.
    """
    was = page.locator(RESULTS).inner_text()
    _ask(page, text)
    page.wait_for_function(
        "(prev) => ((document.querySelector('#frame_MainWindow') || {})"
        ".innerText || '') !== prev",
        arg=was, timeout=LLM_TIMEOUT)
    _wait_for_panel(page)
    _wait_for_results(page)


def _assert_results_painted(page):
    text = page.locator(RESULTS).inner_text()
    assert any(token in text for token in FOUND), (
        f"the results frame names no provider count: {text[:200]!r}")


# An empty result says so in words rather than naming a count, so a helper
# that only parses "<n> provider(s) found" cannot read a legitimate zero.
EMPTY = "No providers matched."


def _total(page):
    text = page.locator(RESULTS).inner_text()
    if EMPTY in text:
        return 0
    for token in FOUND:
        if token in text:
            head = text.split(token)[0].strip().split()[-1]
            return int(head.replace(",", ""))
    pytest.fail(f"the results frame names no provider count: {text[:200]!r}")


def _row_name(page, code):
    """The specialty name the panel shows for one code."""
    return page.locator(f"{PANEL} tr[data-code='{code}'] td") \
               .first.inner_text().strip()


def _card_texts(page):
    return page.locator("[data-testid='provider-card']").all_inner_texts()


def _panel_codes(page):
    return page.evaluate(
        f"() => Array.from(document.querySelectorAll('{PANEL} [data-code]'))"
        ".map(e => e.getAttribute('data-code'))")


@pytest.fixture(scope="module")
def page():
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=["--ignore-certificate-errors"])
    context = browser.new_context(ignore_https_errors=True)
    p = context.new_page()
    p.set_default_timeout(DEFAULT_TIMEOUT)
    p.goto(BASE_URL, wait_until="networkidle")
    p.wait_for_selector("[data-router-action='about_chathealthy']", timeout=DEFAULT_TIMEOUT)
    yield p
    context.close()
    browser.close()
    pw.stop()


@pytest.fixture(scope="module")
def searched(page):
    """One search, shared by the cases that read from it."""
    _ask(page, "find me a shrink in Long Beach CA")
    _wait_for_panel(page)
    _wait_for_results(page)
    _shot(page, "01_after_search")
    return page


class TestTheScreenIsPainted:
    """Every frame that should carry content does."""

    @pytest.mark.parametrize("frame", ["Header", "Footer", "MainWindow",
                                       "LeftPanel", "UserPromptAndControl"])
    def test_frame_exists(self, page, frame):
        expect(page.locator(f"#frame_{frame}")).to_have_count(1)

    def test_specialty_panel_painted(self, searched):
        assert searched.locator(PANEL_BOXES).count() > 0

    def test_results_painted(self, searched):
        _assert_results_painted(searched)


class TestSpecialtyFlagsArePresent:
    """2026-08-25: local was bound to the unversioned SpecialtyMetaData —
    883 rows, no can_prescribe, no is_homeopathic — so the prescriber and
    homeopathic switches toggled an empty set and looked broken."""

    def test_some_rows_are_prescribers(self, searched):
        assert searched.locator(PANEL_TICKED).count() > 0, (
            "no row is ticked: the panel seeds prescribers, so zero ticks "
            "means the rows carry no can_prescribe flag")


class TestApplyFilterDoesNotDestroyTheScreen:
    """2026-08-26: Apply Filter emitted intent_classified, which means 'a
    new query was classified'. NewQueryLoadingWidget answers that by
    blanking LeftPanel, RightPanel and MainWindow — so filtering wiped the
    panel being filtered with."""

    def test_panel_survives(self, searched):
        before = searched.locator(PANEL_BOXES).count()
        assert before > 0
        ticked = _ticked_codes(searched)
        assert len(ticked) > 1, "need two ticks to change the selection and keep one"
        _click_row(searched, ticked[0])
        _apply(searched, 8_000)
        _shot(searched, "02_after_apply")
        assert searched.locator(PANEL_BOXES).count() == before, (
            "Apply Filter destroyed the specialty panel")

    def test_results_still_there(self, searched):
        _assert_results_painted(searched)


class TestTheQueryIsTheTickedSet:
    """2026-08-26: the panel painted prescribers ticked while the search
    used every code the panel offered, so 'find me a shrink' returned
    chiropractors — visible on screen, unticked, and in the results.

    Then the fix for that introduced its own: the prescriber default was
    seeded unconditionally, so Apply Filter wrote the user's choice and the
    default overwrote it on the way back through."""

    def test_reducing_to_one_code_does_not_widen(self, searched):
        """Reduce to one specialty and the result may not grow.

        Deliberately NOT "the total must drop": a code the panel offers can
        have no provider in this geography, so reducing to it correctly
        returns nothing, which a strict-decrease assertion reads as a
        defect.

        And deliberately NOT "every card names the ticked specialty". The
        query matches `taxonomies.code` -- ANY taxonomy the provider holds
        -- while the card displays the PRIMARY one, so a correct result
        routinely shows a different name. NPI 1306138300 carries both
        2084P0800X (primary, displayed as Psychiatry Physician) and
        2084P0804X (Child & Adolescent Psychiatry, the code ticked).
        Asserting the names match reports that correct row as a violation.

        Confirming the returned set against the ticked code needs each
        provider's full taxonomy list, which the card does not carry. This
        case asserts what the screen can actually show; the deterministic
        proof belongs where the taxonomies are readable.
        """
        before = _total(searched)
        ticked = _ticked_codes(searched)
        assert len(ticked) > 1, "need two ticks to prove the query narrows"

        _uncheck_all(searched)
        assert _ticked_codes(searched) == []
        _click_row(searched, ticked[0])
        assert _ticked_codes(searched) == [ticked[0]], (
            "the panel did not hold the single choice that was made")

        _apply(searched, 8_000)
        _shot(searched, "03_one_code")

        after = _total(searched)
        assert after <= before, (
            f"one code of {len(ticked)} returned {after} against {before} "
            f"for the whole set; narrowing widened the result")
        assert _ticked_codes(searched) == [ticked[0]], (
            "the panel came back from Apply Filter with a selection the "
            "user did not make -- a default overwrote the choice")


@pytest.fixture(scope="module")
def paged(page):
    """A result wide enough to page through.

    Its own search rather than the shared one: the narrowing case above
    reduces that result to a single specialty, and pagination inheriting it
    skipped every time -- reporting green while covering nothing. A test
    that needs many rows asks for many rows.
    """
    _new_search(page, "find me a family doctor in California")
    total = _total(page)
    if total <= 25:
        pytest.fail(
            f"the widest query available returns {total}; pagination cannot "
            f"be exercised against this data")
    return page


class TestPagination:
    """2026-08-26: the server had always paged — provider_search takes
    after_npi, the result carries has_more and last_npi — and no op ever
    exposed it, so 337 providers showed 25 with no way to the rest."""

    def test_next_page_control_present_when_more(self, paged):
        expect(paged.locator("[data-testid='providers-next-page']")).to_have_count(1)

    def test_next_page_advances(self, paged):
        before = paged.locator("[data-testid='provider-card']").first.get_attribute("data-npi")
        paged.locator("[data-testid='providers-next-page']").click()
        paged.wait_for_function(
            "(npi) => { const c = document.querySelector"
            "(\"[data-testid='provider-card']\");"
            " return c && c.getAttribute('data-npi') !== npi; }",
            arg=before, timeout=LLM_TIMEOUT)
        _shot(paged, "04_page_two")
        after = paged.locator("[data-testid='provider-card']").first.get_attribute("data-npi")
        assert before != after, "the page did not advance"


class TestProviderRowContent:
    """2026-08-26: each row carried a taxonomy code and nothing saying what
    it means, so a list of 337 gave no way to tell a podiatrist from a
    chiropractor without opening each one."""

    def test_row_names_its_specialty(self, searched):
        card = searched.locator("[data-testid='provider-card']").first
        text = card.inner_text()
        assert "NPI:" in text, "row lost its NPI line"
        lines = [l for l in text.split("\n") if l.strip()]
        assert len(lines) >= 4, (
            f"row should carry name, address, NPI line, NUCC description and "
            f"the detail link; got {len(lines)} lines: {lines}")


DETAIL = "#frame_RightPanel"
DETAIL_CLOSE = f"{DETAIL} [data-testid='provider-detail-close']"


def _open_detail(page):
    """Open the first card's detail and wait for the panel to carry it."""
    npi = page.locator("[data-testid='provider-card']").first.get_attribute("data-npi")
    page.locator(f"[data-router-action='provider:detail'][data-npi='{npi}']") \
        .first.click()
    page.wait_for_function(
        "(npi) => ((document.querySelector('#frame_RightPanel') || {})"
        ".innerText || '').includes(npi)",
        arg=npi, timeout=LLM_TIMEOUT)
    return npi


def _detail_is_open(page):
    return page.locator(DETAIL_CLOSE).count() > 0


def _wait_detail_gone(page):
    page.wait_for_function(
        "() => !document.querySelector"
        "(\"#frame_RightPanel [data-testid='provider-detail-close']\")",
        timeout=LLM_TIMEOUT)


class TestProviderDetailIsDismissable:
    """2026-08-26: the detail had no way out. It painted into RightPanel
    and stayed until a new question, so it outlived the batch it belonged
    to -- and a parameter that only ever got set meant returning to
    FindCare resurrected a panel the user had closed."""

    def test_detail_opens(self, searched):
        npi = _open_detail(searched)
        _shot(searched, "07_detail_open")
        assert npi in searched.locator(DETAIL).inner_text()
        assert _detail_is_open(searched), "the detail has no close control"

    def test_scrolling_within_the_batch_keeps_it(self, searched):
        """The provider is still in the list being presented.

        Scrolling was read literally once and wired to dismiss the panel.
        It is not a dismissal: the batch on screen is the batch on screen
        whether a row is above the fold or below it.
        """
        searched.locator("[data-testid='available-providers']").evaluate(
            "el => { el.scrollTop = el.scrollTop + 400 }")
        searched.wait_for_timeout(1_500)
        assert _detail_is_open(searched), (
            "scrolling took the detail down; the provider is still in the "
            "batch being presented")

    def test_close_control_dismisses_it(self, searched):
        searched.locator(DETAIL_CLOSE).click()
        _wait_detail_gone(searched)
        _shot(searched, "08_detail_closed")
        assert not _detail_is_open(searched)


class TestDetailBelongsToTheBatchOnScreen:
    """The new rule: a detail stays while its provider is in the list being
    presented, and goes when the next batch replaces it."""

    def test_next_batch_dismisses_it(self, paged):
        if paged.locator("[data-testid='providers-next-page']").count() == 0:
            pytest.fail("no further batch to fetch; the rule cannot be exercised")
        _open_detail(paged)                       # a provider on THIS batch
        assert _detail_is_open(paged)
        paged.locator("[data-testid='providers-next-page']").click()
        _wait_detail_gone(paged)
        _shot(paged, "09_detail_cleared_by_next_batch")
        assert not _detail_is_open(paged), (
            "the next batch left a detail on screen for a provider who is "
            "no longer in the list")


class TestContextSwitchRestoresState:
    """2026-08-26: switching to EvaluateCare left FindCare's content on
    screen; switching back lost the page being read and any open detail.

    The requirement is that returning restores the context EXACTLY as it
    was left, so these record the screen before leaving and compare against
    it after returning -- the ticked codes, the total, and the page being
    read. Asserting only that "something painted" would pass on a fresh
    search of the same question, which is the failure it is meant to catch.
    """

    before = {}

    def _goto(self, page, action):
        page.locator("[data-router-action='about_chathealthy']").first.click()
        page.wait_for_selector("text=Build", timeout=DEFAULT_TIMEOUT)
        page.locator(f"[data-router-action='{action}']").first.click()
        page.wait_for_timeout(4_000)

    def test_leaving_blanks_the_content_frames(self, searched):
        assert searched.locator(PANEL_BOXES).count() > 0, "nothing to leave"
        # Leave with a detail OPEN, so returning has one to bring back. The
        # dismissal cases above closed the last one.
        detail_npi = _open_detail(searched)
        type(self).before = {
            "ticked": _ticked_codes(searched),
            "total": _total(searched),
            "first_npi": searched.locator("[data-testid='provider-card']")
                         .first.get_attribute("data-npi"),
            "detail_npi": detail_npi,
        }
        self._goto(searched, "goto_evaluatecare")
        _shot(searched, "05_on_evaluatecare")
        assert searched.locator(PANEL_BOXES).count() == 0, (
            "FindCare's specialty panel is still on screen under "
            "EvaluateCare's ownership")

    def test_returning_restores_the_panel(self, searched):
        self._goto(searched, "goto_findcare")
        searched.wait_for_function(
            f"() => document.querySelectorAll(\"{PANEL_BOXES}\").length > 0",
            timeout=LLM_TIMEOUT)
        _wait_for_results(searched)
        _shot(searched, "06_back_on_findcare")
        assert searched.locator(PANEL_BOXES).count() > 0

    def test_returning_restores_the_ticks(self, searched):
        assert _ticked_codes(searched) == self.before["ticked"], (
            "the panel came back with a different selection than it was "
            "left with")

    def test_returning_restores_the_results(self, searched):
        _assert_results_painted(searched)
        assert _total(searched) == self.before["total"], (
            f"came back to {_total(searched)} providers against "
            f"{self.before['total']} on leaving")

    def test_returning_restores_the_page_being_read(self, searched):
        npi = searched.locator("[data-testid='provider-card']") \
                      .first.get_attribute("data-npi")
        assert npi == self.before["first_npi"], (
            f"came back to a list starting at {npi} against "
            f"{self.before['first_npi']} on leaving -- the stored cursor "
            f"did not take the query back to that page")

    def test_returning_restores_the_open_detail(self, searched):
        searched.wait_for_function(
            "(npi) => ((document.querySelector('#frame_RightPanel') || {})"
            ".innerText || '').includes(npi)",
            arg=self.before["detail_npi"], timeout=LLM_TIMEOUT)
        _shot(searched, "10_detail_restored")
        assert self.before["detail_npi"] in searched.locator(DETAIL).inner_text()

    def test_a_closed_detail_does_not_come_back(self, searched):
        searched.locator(DETAIL_CLOSE).click()
        searched.wait_for_function(
            "() => !document.querySelector"
            "(\"#frame_RightPanel [data-testid='provider-detail-close']\")",
            timeout=DEFAULT_TIMEOUT)
        self._goto(searched, "goto_evaluatecare")
        self._goto(searched, "goto_findcare")
        _wait_for_results(searched)
        searched.wait_for_timeout(3_000)
        _shot(searched, "11_closed_detail_stays_closed")
        assert not _detail_is_open(searched), (
            "a detail the user closed came back on returning -- the "
            "parameter records that one was opened once, not that one is "
            "open")


class TestSharedServicesAndSessionInfo:
    """2026-08-26: the SharedServices button closed the window it was in
    rather than saying where you are; and there was no way to see session
    state after the session view was removed."""

    def test_shared_services_states_where_you_are(self, page):
        page.locator("[data-router-action='about_chathealthy']").first.click()
        page.wait_for_selector("text=Build", timeout=DEFAULT_TIMEOUT)
        page.locator("[data-router-action='goto_sharedservices']").first.click()
        page.wait_for_timeout(1_500)
        expect(page.locator("[data-router-action='goto_sharedservices']").first
               ).to_contain_text("You are in SharedServices")

    def test_session_info_shows_the_live_parameters(self, page):
        page.locator("[data-router-action='session_info']").first.click()
        page.wait_for_selector("text=User Parameters", timeout=LLM_TIMEOUT)
        _shot(page, "07_session_info")
        body = page.locator("text=User Parameters").first
        expect(body).to_be_visible()
