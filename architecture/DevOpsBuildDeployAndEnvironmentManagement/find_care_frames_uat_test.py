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

# Navigation waits for the document, never for the network to fall idle.
# /gate answers with a streamed NDJSON response and the wrapper opens one
# on load, so a connection is deliberately held open and "networkidle"
# can never be reached -- it timed out on every test in this file. What
# says the page is ready is the readiness selector each fixture waits for
# immediately after navigating, which is unchanged.
BASE_URL = os.getenv("SMOKE_TEST_URL", "https://localhost")
DEFAULT_TIMEOUT = 60_000
LLM_TIMEOUT = 240_000          # a turn runs normalize + embed + vector + filter

SCREENSHOT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "_oneshots", "test_output", "frames_uat",
)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# A window is a declared frame in the top document, like the seven layout
# frames. It is NOT inside the MainWindow iframe: that iframe is a 1x1
# offscreen host for the React widgets, so anything rendered there has no
# viewport and cannot be seen or clicked. A test looking in the wrong place
# proves nothing, which is why these assert visibility and geometry rather
# than presence in the DOM.
ABOUT_WINDOW = "#frame_AboutChatHealtyPopUP"
SESSION_WINDOW = "#frame_SessionInfoPopUp"


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
    """The count on screen, once the turn that produces it has finished.

    Reading without waiting caught the results frame mid-search showing
    "Waiting for response..." and reported it as a missing count. A search
    is asynchronous; the number is only there when it lands.
    """
    page.wait_for_function(
        "() => { const t = (document.querySelector('#frame_MainWindow')"
        f" || {{}}).innerText || ''; return {_FOUND_JS}"
        f" || t.includes('{EMPTY}'); }}",
        timeout=LLM_TIMEOUT)
    text = page.locator(RESULTS).inner_text()
    if EMPTY in text:
        return 0
    for token in FOUND:
        if token in text:
            head = text.split(token)[0].strip().split()[-1]
            return int(head.replace(",", ""))
    pytest.fail(f"the results frame names no provider count: {text[:200]!r}")


@pytest.fixture(scope="module")
def page():
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=["--ignore-certificate-errors"])
    context = browser.new_context(ignore_https_errors=True)
    p = context.new_page()
    p.set_default_timeout(DEFAULT_TIMEOUT)
    p.goto(BASE_URL, wait_until="domcontentloaded")
    p.wait_for_selector("[data-router-action='about_chathealthy']", timeout=DEFAULT_TIMEOUT)
    yield p
    context.close()
    browser.close()
    pw.stop()


@pytest.fixture(scope="class")
def searched(page):
    """One search per CLASS, not per module.

    Shared across the module it made every class depend on the order it ran
    in: a class that ticked a chip, opened a detail or paged forward left
    that state for the next one, which was asserting against an unfiltered
    result. Six failures were that and nothing else, and reordering only
    moved which six.

    A search per class costs seconds and buys independence -- each class
    starts from a session nothing else has touched.
    """
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("[data-router-action='about_chathealthy']",
                           timeout=DEFAULT_TIMEOUT)
    _ask(page, "find me a shrink in Long Beach CA")
    _wait_for_panel(page)
    _wait_for_results(page)
    _shot(page, "01_after_search")
    return page


@pytest.fixture(scope="class")
def fresh(page):
    """A session with nothing established.

    The ladder starts from "we know nothing", so it cannot run on the page
    the other cases have been searching on -- their results are still up,
    and the first turn asserts there are none.
    """
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("[data-router-action='about_chathealthy']",
                           timeout=DEFAULT_TIMEOUT)
    return page


class TestItAsksForWhatIsMissing:
    """2026-08-26, from a live session. Three turns, each giving the system
    a little more, and each answer asking only for what is still absent.

    The second turn is the one that matters: two things were missing, so it
    asked for both -- and did not re-ask what it had already been told.
    That is the rule holding across turns, which a single-turn case cannot
    show.

    Prose is not asserted; it comes from a model and varies. What is
    asserted is the shape: nothing routes until enough is known, the
    question advances each turn, and the system never blames the person.
    """

    def test_an_utterance_it_cannot_route_asks_what_you_want(self, fresh):
        before = _settled_text(fresh)
        _ask(fresh, "It is a windy day today.")
        reply = _system_reply(fresh, before)
        assert fresh.locator("[data-testid='provider-card']").count() == 0, (
            "an utterance carrying no healthcare request produced results")
        low = reply.lower()
        for word in ACCUSING:
            assert word not in low, (
                f"the reply says {word!r} -- that is a claim about the "
                f"person, not about us")

    def test_a_partial_request_asks_for_the_rest(self, fresh):
        before = _settled_text(fresh)
        _ask(fresh, "I'm looking for a provider")
        reply = _system_reply(fresh, before)
        assert fresh.locator("[data-testid='provider-card']").count() == 0, (
            "a request with no complaint and no place produced results")
        # Which missing thing it asks for varies run to run: sometimes both
        # the complaint and the place, sometimes the complaint alone. What
        # must hold is that it asks for something still absent rather than
        # repeating a question already answered. Pinning it to the place
        # made this case pass or fail on the model's mood.
        low = reply.lower()
        asks_place = any(w in low for w in ("where", "located", "location",
                                            "city", "state", "zip"))
        asks_complaint = any(w in low for w in ("concern", "condition",
                                                "complaint", "kind of",
                                                "what type", "symptom",
                                                "help you with"))
        assert asks_place or asks_complaint, (
            f"neither the place nor the complaint is known, and the reply "
            f"asks for neither: {reply[-300:]!r}")
        for word in ACCUSING:
            assert word not in low, f"the reply says {word!r}"

    def test_the_complete_request_routes(self, fresh):
        _ask(fresh, "I need someone to help me with my chronic headaches "
                    "and I'm in Long Beach NY")
        _wait_for_panel(fresh)
        _wait_for_results(fresh)
        _shot(fresh, "12_clarification_ladder_resolved")
        _assert_results_painted(fresh)


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


@pytest.fixture(scope="class")
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
    """2026-08-26: the server had always paged — provider_search takes a
    keyset cursor and a direction, and the result carries has_more,
    first_npi and last_npi — and no op ever exposed it, so 337 providers
    showed 25 with no way to the rest."""

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
    """Open the first card's detail and wait for it to actually be there.

    Waiting for the NPI to appear is not enough: the loading screen reads
    "Loading NPI 1234", so the wait passed on a panel that had not loaded
    and the next assertion looked for a close control only the loaded panel
    carries. That control IS the signal the detail arrived.
    """
    npi = page.locator("[data-testid='provider-card']").first.get_attribute("data-npi")
    link = page.locator(f"[data-router-action='provider:detail'][data-npi='{npi}']")
    link.first.click()
    page.wait_for_selector(DETAIL_CLOSE, timeout=LLM_TIMEOUT)
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


PROMPT_TEXT = "#frame_MainWindow, #frame_UserPromptAndControl"

# Words that make the claim about the person rather than about us. The
# system did not understand; that is a fact about the system.
ACCUSING = ("nonsense", "gibberish", "unintelligible", "invalid",
            "sorry", "error", "failed", "cannot understand")


def _settled_text(page) -> str:
    """Body text with the running turn timer removed.

    The timer ticks 1s, 2s, 3s while the model is working, so the page
    changes every second whether or not an answer has arrived. A wait that
    only asks "did anything change" is satisfied by the clock and reads the
    PREVIOUS reply -- which is how this suite reported the app re-asking a
    question it had in fact answered correctly.
    """
    return page.evaluate(
        "() => (document.body.innerText || '').split(String.fromCharCode(10))"
        ".filter(line => { const s = line.trim();"
        "   if (!s.endsWith('s')) return true;"
        "   const n = s.slice(0, -1);"
        "   if (!n.length) return true;"
        "   return ![...n].every(c => c >= '0' && c <= '9'); })"
        ".join(String.fromCharCode(10))")


def _system_reply(page, before: str) -> str:
    """The prose the system put on screen in answer to the last utterance.

    Waits for the text to differ from `before` ignoring the timer, and then
    to hold still, so what is read is the finished answer.
    """
    deadline = LLM_TIMEOUT
    waited = 0
    last = None
    while waited < deadline:
        now = _settled_text(page)
        if now != before and now == last:
            return now
        last = now
        page.wait_for_timeout(1_000)
        waited += 1_000
    pytest.fail("the system never answered")


class TestSharedServicesAndSessionInfo:
    """2026-08-26: the SharedServices button closed the window it was in
    rather than saying where you are; and there was no way to see session
    state after the session view was removed."""

    def _open_about(self, page):
        page.locator("[data-router-action='about_chathealthy']").first.click()
        expect(page.locator(ABOUT_WINDOW)).to_be_visible(timeout=DEFAULT_TIMEOUT)

    def _open_session(self, page):
        self._open_about(page)
        page.locator("[data-router-action='session_info']").first.click()
        expect(page.locator(SESSION_WINDOW)).to_be_visible(timeout=LLM_TIMEOUT)
        # The window paints "Loading SharedServices..." first; the session
        # arrives when the call returns. Waiting on the loading state and
        # then asserting reads an empty window and blames the feature.
        expect(page.locator(f"{SESSION_WINDOW} >> text=User Parameters").first
               ).to_be_visible(timeout=LLM_TIMEOUT)
        expect(page.locator(f"{SESSION_WINDOW} th").first
               ).to_be_visible(timeout=LLM_TIMEOUT)

    def test_shared_services_states_where_you_are(self, page):
        self._open_about(page)
        page.locator("[data-router-action='goto_sharedservices']").first.click()
        expect(page.locator("[data-router-action='goto_sharedservices']").first
               ).to_contain_text("You are in SharedServices",
                                 timeout=DEFAULT_TIMEOUT)

    def test_session_info_shows_the_live_parameters(self, page):
        self._open_session(page)
        _shot(page, "07_session_info")
        expect(page.locator(f"{SESSION_WINDOW} >> text=User Parameters").first
               ).to_be_visible()

    def test_a_window_opened_from_a_window_does_not_cover_it(self, page):
        """About offers Session Info, so both are open at once. They used to
        be built at the same fixed centre, which put the second on top of the
        first; each is a declared frame with its own place now."""
        self._open_session(page)
        about = page.locator(ABOUT_WINDOW).bounding_box()
        session = page.locator(SESSION_WINDOW).bounding_box()
        assert about and session, "a window is open but has no box"
        assert (about["x"] + about["width"] <= session["x"]
                or session["x"] + session["width"] <= about["x"]), (
            f"the windows overlap: about={about} session={session}")

    def test_the_wrapper_builds_no_window(self, page):
        """ClientRouter used to manufacture the popup, which is display
        authored outside React. The frames are markup now and CSS hides an
        empty one, so the wrapper composes nothing."""
        self._open_session(page)
        assert page.evaluate(
            '() => document.querySelectorAll(\'[id^="popup_"]\').length'
        ) == 0, "the wrapper is still building the window"

    def test_session_states_the_build_every_server_carries(self, page):
        """A server reporting one build while running the code of another is
        what cost a day; the section exists so that is visible without asking
        each server by hand."""
        self._open_session(page)
        expect(page.locator(f"{SESSION_WINDOW} >> text=Deployment Facts").first
               ).to_be_visible(timeout=DEFAULT_TIMEOUT)
        headers = page.locator(f"{SESSION_WINDOW} th").all_inner_texts()
        for column in ("Deployed", "Running"):
            assert column in headers, f"Deployment Facts has no {column} column"
        assert page.locator(f"{SESSION_WINDOW} tbody tr").count() > 0, (
            "Deployment Facts names no server")

    def test_the_session_offers_a_pdf_a_person_can_press(self, page):
        """Visible and pressable, not merely present: the window spent an
        afternoon rendering into a 1x1 offscreen iframe where every control
        existed in the DOM and none could be pressed."""
        self._open_session(page)
        pdf = page.locator("[data-router-action='session_pdf']").first
        expect(pdf).to_be_visible(timeout=DEFAULT_TIMEOUT)
        box = pdf.bounding_box()
        assert box and box["x"] >= 0 and box["y"] >= 0 and box["width"] > 0, (
            f"the PDF control is not on the screen: {box}")
        # Chrome, not content: it must not print itself onto the page.
        assert pdf.get_attribute("data-print-omit") is not None


class TestTheSessionWindowAndTheDisambiguationTurn:
    """2026-08-28. Five things the operator asked for, each asserted against
    what a person can see rather than what is in the DOM.

    The one that matters most: answering "yes" to "did you mean California?"
    replaced a psychiatry panel of 45 codes with 57 that had chiropractors in
    it and no Psychologist. UM had asked a geography question, and the answer
    to it was scored as a description of care. A complaint that did not change
    asks the specialty filter nothing, so it is not handed off to.
    """

    def _open_session(self, page):
        page.locator("[data-router-action='about_chathealthy']").first.click()
        expect(page.locator(ABOUT_WINDOW)).to_be_visible(timeout=DEFAULT_TIMEOUT)
        page.locator("[data-router-action='session_info']").first.click()
        expect(page.locator(SESSION_WINDOW)).to_be_visible(timeout=LLM_TIMEOUT)
        expect(page.locator(f"{SESSION_WINDOW} >> text=Deployment Facts").first
               ).to_be_visible(timeout=LLM_TIMEOUT)

    def test_answering_the_question_does_not_repaint_the_panel(self, fresh):
        """The panel a person is reading survives their answer to a question
        about geography. 45 became 57 before this."""
        _ask(fresh, "find me a shrink in san fransisco")
        _wait_for_panel(fresh)
        before = _rows(fresh)
        _ask(fresh, "yes")
        fresh.wait_for_timeout(20_000)
        after = _rows(fresh)
        assert [c for c, _ in after] == [c for c, _ in before], (
            f"the panel was repainted: {len(before)} codes became {len(after)}; "
            f"added={sorted(set(c for c, _ in after) - set(c for c, _ in before))[:6]} "
            f"dropped={sorted(set(c for c, _ in before) - set(c for c, _ in after))[:6]}")

    def test_an_utterance_that_keeps_the_complaint_leaves_the_panel_alone(self, fresh):
        """There IS an utterance here, and it still must not repaint. "now
        only the male ones" narrows the search already in force; the kind of
        care wanted did not move, so the specialty filter is not handed off
        to and the panel is untouched -- same codes, same ticks, same order.

        Ticks matter as much as codes. A panel that comes back with the same
        rows but everything re-checked has thrown away the narrowing the
        person did, which reads to them as the same bug."""
        _ask(fresh, "find me a shrink in Los Angeles")
        _wait_for_panel(fresh)
        _wait_for_results(fresh)
        before = _rows(fresh)
        assert before, "no panel to leave alone"

        _new_search_settle = 20_000
        _ask(fresh, "now only the male ones")
        fresh.wait_for_timeout(_new_search_settle)

        after = _rows(fresh)
        assert after == before, (
            "the panel moved on a turn that did not change the complaint: "
            f"{len(before)} rows -> {len(after)}; "
            f"codes added={sorted(set(c for c, _ in after) - set(c for c, _ in before))[:6]} "
            f"dropped={sorted(set(c for c, _ in before) - set(c for c, _ in after))[:6]}; "
            f"ticks before={sum(1 for _, t in before if t)} "
            f"after={sum(1 for _, t in after if t)}")

    def test_applying_a_filter_does_not_repaint_the_panel(self, fresh):
        """No utterance, so no new complaint, so the specialty filter is not
        handed off to at all. Unticking boxes and applying used to re-derive
        the panel: 48 possible became 34, with Sleep Medicine, Neurology and
        Epilepsy appearing in a list the person had already narrowed."""
        _ask(fresh, "find me a shrink in Los Angeles")
        _wait_for_panel(fresh)
        _wait_for_results(fresh)
        before = [code for code, _ in _rows(fresh)]
        assert before, "no panel to filter"

        # Untick two rows -- a change to the selection, not to the question.
        for code in [c for c, ticked in _rows(fresh) if ticked][:2]:
            _click_row(fresh, code)
        _apply(fresh, 12_000)

        after = [code for code, _ in _rows(fresh)]
        assert after == before, (
            f"the panel was re-derived: {len(before)} rows became {len(after)}; "
            f"added={sorted(set(after) - set(before))[:6]} "
            f"dropped={sorted(set(before) - set(after))[:6]}")

    def test_a_window_opens_in_the_centre(self, page):
        self._open_session(page)
        box = page.locator(SESSION_WINDOW).bounding_box()
        view = page.viewport_size
        assert box and view, "the window has no box"
        centre = box["x"] + box["width"] / 2
        assert abs(centre - view["width"] / 2) < 40, (
            f"the window is not centred: centre={centre} of {view['width']}")

    def test_a_person_can_move_a_window(self, page):
        self._open_session(page)
        handle = page.locator(f"{SESSION_WINDOW} .ch-popup-drag").first
        expect(handle).to_be_visible(timeout=DEFAULT_TIMEOUT)
        start = page.locator(SESSION_WINDOW).bounding_box()
        page.mouse.move(start["x"] + 60, start["y"] + 10)
        page.mouse.down()
        page.mouse.move(start["x"] + 260, start["y"] + 120, steps=12)
        page.mouse.up()
        page.wait_for_timeout(300)
        end = page.locator(SESSION_WINDOW).bounding_box()
        assert abs(end["x"] - start["x"]) > 80 or abs(end["y"] - start["y"]) > 40, (
            f"the window did not move: {start} -> {end}")

    def test_the_session_window_scrolls(self, page):
        """It read as truncated because it could not scroll."""
        self._open_session(page)
        sel = SESSION_WINDOW
        overflows = page.evaluate(
            "(s) => { const e = document.querySelector(s);"
            " return e.scrollHeight > e.clientHeight + 4; }", sel)
        if not overflows:
            return   # nothing to scroll is not a failure to scroll
        page.evaluate("(s) => { document.querySelector(s).scrollTop = 400; }", sel)
        moved = page.evaluate("(s) => document.querySelector(s).scrollTop", sel)
        assert moved > 0, "the window holds more than it shows and will not scroll"

    def test_deployment_facts_names_components_and_builds(self, page):
        """Component names, not the URLs that serve them, and the build. No
        other column."""
        self._open_session(page)
        text = page.locator(f"{SESSION_WINDOW}").inner_text()
        section = text[text.find("Deployment Facts"):]
        section = section[:section.find("User Parameters")]
        for component in ("FindCare Server", "EvaluateCare Server",
                          "SharedServices Server"):
            assert component in section, f"{component} is not named"
        for unwanted in ("target_", "Deployed", "Running", "Commit", "http"):
            assert unwanted not in section, (
                f"Deployment Facts still shows {unwanted!r}")


# ── State-changing cases run last ───────────────────────────────────
# Everything above shares one browser session through the module-scoped
# `page`. These two switch a filter ON and leave it on, so run earlier they
# narrowed the list every later class was asserting against -- six failures
# that were this ordering and nothing else. Each takes its own fresh
# session; putting them at the end keeps the shared one clean for the rest.

REFINE_CHIP = f"{PANEL} [data-router-action='filter:refine']"


def _chips(page):
    """[(dimension, value, count)] for every refinement the panel offers."""
    return page.evaluate(
        f"() => Array.from(document.querySelectorAll(\"{REFINE_CHIP}\")).map(e => ["
        "e.getAttribute('data-dim'), e.getAttribute('data-value'),"
        " parseInt((e.querySelector('b') || {}).textContent || '0', 10)])")


class TestThePanelSaysHowToNarrow:
    """2026-08-26: the panel is where the system messages the person about
    the result they are looking at. It offers what is still narrowable and
    what each choice would cost, counted over THIS result -- so a
    preference is made with its price visible rather than discovered after.

    The counts must reconcile with the total. Two bugs shipped before this
    case existed: insurance counted identifier rows rather than providers
    (108 shown where 97 qualified), and sole-proprietor dropped the
    "Not answered" records so its numbers did not sum to the result."""

    def test_the_panel_offers_narrowings(self, searched):
        chips = _chips(searched)
        assert chips, "the panel offers no way to narrow a result of hundreds"
        dims = {d for d, _, _ in chips}
        assert "provider_sex" in dims, f"sex is always offerable; got {dims}"

    def test_every_choice_shows_its_cost(self, searched):
        for dim, value, count in _chips(searched):
            assert count > 0, f"{dim}={value} offered with a count of {count}"

    def test_sex_counts_reconcile_with_the_total(self, searched):
        rows = [(v, n) for d, v, n in _chips(searched) if d == "provider_sex"]
        assert rows, "no sex choices offered"
        assert sum(n for _, n in rows) == _total(searched), (
            f"sex counts {rows} do not sum to the {_total(searched)} on screen")

    def test_choosing_a_narrowing_narrows(self, searched):
        before = _total(searched)
        rows = [(v, n) for d, v, n in _chips(searched) if d == "provider_sex"]
        value, expected = sorted(rows, key=lambda r: r[1])[0]
        searched.locator(
            f"{PANEL} [data-router-action='filter:refine'][data-value='{value}']"
        ).first.click()
        searched.wait_for_function(
            "(prev) => { const t = (document.querySelector('#frame_MainWindow')"
            " || {}).innerText || ''; return t && !t.includes(prev); }",
            arg=f"{before} providers found", timeout=LLM_TIMEOUT)
        _shot(searched, "13_refined_by_sex")
        after = _total(searched)
        assert after == expected, (
            f"the panel promised {expected} for sex={value} and the search "
            f"returned {after}")

    def test_the_chosen_one_stays_on_the_panel(self, searched):
        """A filter you cannot see is a filter you cannot remove.

        The chosen dimension used to vanish from the panel once applied, so
        the toggle existed on the server and nothing on screen could reach
        it.
        """
        in_force = searched.locator(
            f"{PANEL} [data-router-action='filter:refine'][data-in-force='1']")
        assert in_force.count() >= 1, (
            "the narrowing in force is not shown, so it cannot be undone")

    def test_choosing_it_again_clears_it(self, searched):
        narrowed = _total(searched)
        chip = searched.locator(
            f"{PANEL} [data-router-action='filter:refine'][data-in-force='1']").first
        value = chip.get_attribute("data-value")
        chip.click()
        searched.wait_for_function(
            "(prev) => { const t = (document.querySelector('#frame_MainWindow')"
            " || {}).innerText || ''; return t && !t.includes(prev); }",
            arg=f"{narrowed} providers found", timeout=LLM_TIMEOUT)
        _shot(searched, "14_refinement_cleared")
        assert _total(searched) > narrowed, (
            f"clearing sex={value} left the result at {narrowed}; it should "
            f"widen back")


def _chip_in_force(page, value: str) -> bool:
    return page.locator(
        f"{PANEL} [data-router-action='filter:refine']"
        f"[data-value='{value}'][data-in-force='1']").count() > 0


class TestACountySearch:
    """Never covered until now, which is why it broke unseen.

    Every case in this file until this one used city+state. A county goes
    down a different route, and refinements were wired into one route only
    -- so the suite was green while a county search returned providers with
    no way to narrow them at all.
    """

    @pytest.fixture(scope="class")
    def county(self, page):
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("[data-router-action='about_chathealthy']",
                               timeout=DEFAULT_TIMEOUT)
        _ask(page, "find me a shrink in Los Angeles County CA")
        _wait_for_panel(page)
        _wait_for_results(page)
        _shot(page, "16_county_search")
        return page

    def test_a_county_returns_providers(self, county):
        assert _total(county) > 0, (
            "Los Angeles County is among the densest provider areas in the "
            "country and returned nothing")

    def test_the_panel_is_painted(self, county):
        assert county.locator(PANEL_BOXES).count() > 0, "no specialty panel"

    def test_prescribers_are_ticked(self, county):
        assert county.locator(PANEL_TICKED).count() > 0, (
            "no row is ticked: the panel seeds prescribers, so zero means "
            "the specialty rows reached it without can_prescribe")

    def test_the_panel_says_how_to_narrow(self, county):
        chips = _chips(county)
        assert chips, (
            "a county search offers no refinements -- the route returns "
            "providers and the panel has nothing to say about them")

    def test_the_counts_reconcile(self, county):
        rows = [(v, n) for d, v, n in _chips(county) if d == "provider_sex"]
        assert rows, "no sex choices offered"
        assert sum(n for _, n in rows) == _total(county), (
            f"sex counts {rows} do not sum to the {_total(county)} on screen")


class TestSayingItOutLoudNarrows:
    """The chips are one way in; speech is the other. A person who says
    "a female doctor" should get the same narrowing as one who clicks it,
    because both write the same parameter.

    Its own session: the ladder and the panel cases leave preferences set,
    and this asserts against a result that has none.
    """

    @pytest.fixture(scope="class")
    def spoken(self, page):
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("[data-router-action='about_chathealthy']",
                               timeout=DEFAULT_TIMEOUT)
        _ask(page, "find me a psychiatrist in New Orleans LA")
        _wait_for_panel(page)
        _wait_for_results(page)
        return page

    def test_a_spoken_sex_preference_is_applied(self, spoken):
        """Saying it does the same as clicking it.

        Deliberately NOT "the total equals the count the panel showed
        BEFORE the utterance". A spoken turn is classified afresh and can
        re-resolve its specialty set -- and if the geography validator
        rejects the first answer, the retry may resolve differently again.
        Comparing across two turns' bases measured the classifier's
        variance, not whether the preference applied.

        What must hold is self-consistent: after the turn, the choice shown
        as in force is the one the result was actually filtered by, so its
        count IS the total on screen.
        """
        _ask(spoken, "I would prefer a female doctor")
        spoken.wait_for_function(
            f"() => document.querySelector(\"{PANEL} "
            "[data-router-action='filter:refine'][data-in-force='1']\") !== null",
            timeout=LLM_TIMEOUT)
        _shot(spoken, "15_spoken_sex_preference")

        in_force = [(d, v, n) for d, v, n in _chips(spoken)
                    if _chip_in_force(spoken, v)]
        assert in_force, "nothing is in force after stating a preference"
        dim, value, count = in_force[0]
        assert dim == "provider_sex" and value == "F", (
            f"'a female doctor' put {dim}={value} in force")
        assert count == _total(spoken), (
            f"the panel shows {count} for the filter in force and the result "
            f"holds {_total(spoken)}")

    def test_the_spoken_preference_shows_on_the_panel(self, spoken):
        in_force = spoken.locator(
            f"{PANEL} [data-router-action='filter:refine'][data-in-force='1']")
        assert in_force.count() >= 1, (
            "a preference stated in words is not shown on the panel, so it "
            "cannot be seen or removed")


# ─────────────────────────────────────────────────────────────────────
# EPIC-006-F-006 / F-007 — a facility is a place, not a person.
#
# A facility search returned rows on the wire while the browser painted
# nothing: the timer sat and expired because no component knew the
# `facilities` kind. These assert what a person sees, which is the only
# evidence that the feature exists.
# ─────────────────────────────────────────────────────────────────────

FACILITY_CARD = f"{RESULTS} [data-testid='facility-card']"
FACILITY_FOUND = ("facilities found", "facility found")
_FACILITY_FOUND_JS = " || ".join(
    f"t.includes('{token}')" for token in FACILITY_FOUND)


def _wait_for_facilities(page):
    """The facility list is on screen, not the searching indicator.

    Waiting on the card rather than on a count, because the count is the
    heading and a card is the thing a person reads.
    """
    page.wait_for_function(
        f"() => document.querySelectorAll(\"{FACILITY_CARD}\").length > 0",
        timeout=LLM_TIMEOUT)


@pytest.fixture(scope="class")
def facilities(page):
    """One facility search per class, from a session nothing has touched."""
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("[data-router-action='about_chathealthy']",
                           timeout=DEFAULT_TIMEOUT)
    _ask(page, "find me an urgent care clinic in Los Angeles CA")
    _wait_for_facilities(page)
    _shot(page, "20_facility_results")
    return page


class TestAFacilitySearchPaintsSomething:
    """The defect: 105 urgent care clinics on the wire, a blank screen and
    a timer counting to nothing."""

    def test_the_results_frame_shows_facility_rows(self, facilities):
        cards = facilities.locator(FACILITY_CARD)
        assert cards.count() > 0, (
            "a facility search painted no rows; the person sees nothing")

    def test_the_searching_timer_is_gone(self, facilities):
        timer = facilities.locator(
            f"{RESULTS} [data-testid='facility-searching-timer']")
        assert timer.count() == 0, (
            "the searching indicator is still on screen after the results "
            "arrived, so the turn looks to the person like it never ended")

    def test_the_heading_names_a_facility_count(self, facilities):
        text = facilities.locator(RESULTS).inner_text()
        assert any(token in text for token in FACILITY_FOUND), (
            f"the results frame names no facility count: {text[:200]!r}")


class TestTheFacilityRowShowsExactlyFiveThings:
    """EPIC-006-F-006-S-002-REQ-B-007 fixes the row exactly. A sixth thing
    on it is a defect, not a bonus."""

    FIVE = ("facility-name", "facility-address-count", "facility-address",
            "facility-type", "facility-detail-link")

    def test_the_row_shows_each_of_the_five(self, facilities):
        row = facilities.locator(FACILITY_CARD).first
        for testid in self.FIVE:
            assert row.locator(f"[data-testid='{testid}']").count() == 1, (
                f"the row does not show {testid}, which the requirement names")

    def test_the_row_shows_nothing_else(self, facilities):
        """Every marked element in the row is one of the five."""
        marked = facilities.evaluate(
            "() => { const r = document.querySelector"
            f"(\"{FACILITY_CARD}\");"
            " return r ? Array.from(r.querySelectorAll('[data-testid]'))"
            ".map(e => e.getAttribute('data-testid')) : []; }")
        extra = [m for m in marked if m not in self.FIVE]
        assert extra == [], (
            f"the row shows {extra} beyond the five the requirement fixes")

    def test_the_row_names_the_facility_and_its_kind(self, facilities):
        row = facilities.locator(FACILITY_CARD).first
        name = row.locator("[data-testid='facility-name']").inner_text().strip()
        assert name, "the row names no facility"
        count = row.locator(
            "[data-testid='facility-address-count']").inner_text()
        assert "practice address" in count, (
            f"the row does not say how many practice addresses: {count!r}")


class TestTheFacilityDetailOpensFromTheRow:
    """The link is the fifth thing on the row, and it has to go somewhere."""

    def test_clicking_the_link_opens_the_detail(self, facilities):
        facilities.locator(
            f"{FACILITY_CARD} [data-testid='facility-detail-link']").first.click()
        facilities.wait_for_function(
            "() => document.querySelector"
            "(\"[data-testid='facility-identity']\") !== null",
            timeout=LLM_TIMEOUT)
        _shot(facilities, "21_facility_detail")

    def test_the_detail_names_the_organization(self, facilities):
        legal = facilities.locator("[data-testid='facility-legal-name']")
        assert legal.count() == 1, "the detail shows no legal business name"
        assert legal.inner_text().strip(), "the legal business name is empty"

    def test_the_official_is_labelled_as_the_authorized_official(self, facilities):
        block = facilities.locator("[data-testid='authorized-official']")
        assert block.count() == 1, (
            "the detail shows no authorized official section")
        label = block.inner_text()
        assert "authorized official" in label.lower(), (
            "the section is not labelled as the record's authorized "
            f"official, so it names one person with another's role: {label[:120]!r}")


class TestTheProviderJourneyStillWorks:
    """The facility work must not regress the care-giver path."""

    def test_a_shrink_search_still_paints_the_panel_and_the_list(self, page):
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("[data-router-action='about_chathealthy']",
                               timeout=DEFAULT_TIMEOUT)
        _ask(page, "find me a shrink in Long Beach CA")
        _wait_for_panel(page)
        _wait_for_results(page)
        _shot(page, "22_provider_journey_intact")
        assert _total(page) > 0, "the provider search returned nothing"
        assert page.locator(PANEL_BOXES).count() > 0, (
            "the specialty panel painted no rows")
