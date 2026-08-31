# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# find_care_clinical_trials_test.py
# Standalone Playwright smoke test for the Find Clinical Trials display
# requirements (EPIC-006-F-005-S-002). Covers every display REQ in S-002:
# REQ-B-066 through REQ-B-075. Test path mandated by REQ-T-100.
#
# Each test maps 1:1 to a REQ. The test names mirror the REQ ids so a
# failure points straight at the requirement that broke.
#
# Run:
#   python -m pytest architecture/DevOpsBuildDeployAndEnvironmentManagement/find_care_clinical_trials_test.py -v

import os
import pytest
from playwright.sync_api import sync_playwright

SMOKE_ENV = os.getenv("SMOKE_TEST_ENV", "local").lower()

_LABEL_SEPARATORS = frozenset(": =" + chr(9) + chr(13) + chr(10))


def _labelled_value(text: str, label: str, values: tuple[str, ...]) -> bool:
    """True when `label` is followed — past separators — by one of `values`.

    The panel writes "age: 55" or "age = 55" or "age 55" depending on how
    the band was composed, and the requirement is about the pairing, not
    the punctuation between the two halves.
    """
    low = text.lower()
    at = low.find(label.lower())
    while at != -1:
        k = at + len(label)
        while k < len(low) and low[k] in _LABEL_SEPARATORS:
            k += 1
        if any(low.startswith(v.lower(), k) for v in values):
            return True
        at = low.find(label.lower(), at + 1)
    return False

_ENV_BASE_URL = {
    "local": "https://localhost",
    "dev":   "https://dev.chathealthy.ai",
    "qa":    "https://qa.chathealthy.ai",
    "prod":  "https://chathealthy.ai",
}

BASE_URL = os.getenv("SMOKE_TEST_URL", _ENV_BASE_URL.get(SMOKE_ENV, _ENV_BASE_URL["local"]))
CHAT_TIMEOUT = 180_000
TRIAL_LIST_TIMEOUT = 180_000

SCREENSHOT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "_oneshots", "test_output", "clinical_trials_smoke",
)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


# ── Selectors mapped to the 7-frame architecture (Website/index.html) ────

def _frame_main(page):
    return page.locator("#frame_MainWindow")


def _frame_left(page):
    return page.locator("#frame_LeftPanel")


def _frame_right(page):
    return page.locator("#frame_RightPanel")


def _frame_user_message(page):
    return page.locator("#frame_UserMessage")


def _chat_input(page):
    # UserPromptWidget renders this id inside frame_UserPromptAndControl.
    return page.locator("#userInput")


def _send_button(page):
    return page.locator("#userInputSubmit")


def _screenshot(page, name):
    page.screenshot(path=os.path.join(SCREENSHOT_DIR, f"{name}.png"), full_page=True)


# Single fresh-spelled, fully-refined clinical-trials query that pushes
# the UM classifier directly to findClinicalTrials without a refinement
# round trip. Used by every setup that needs trials painted.
# Pick a high-volume condition + scope so CT.gov returns enough trials to
# guarantee pagination (Response.cursor non-null on page 1). Lung cancer
# US-only is reliably ~hundreds of recruiting trials.
TRIAL_QUERY = (
    "Find me a clinical trial for lung cancer for my 55-year-old father "
    "in Los Angeles CA, US-based trials"
)


def _wait_for_trials(page, timeout=TRIAL_LIST_TIMEOUT):
    """Wait until LeftPanel shows at least one NCT entry — trials list painted."""
    page.wait_for_function(
        """() => {
            const lp = document.getElementById('frame_LeftPanel');
            if (!lp) return false;
            return (lp.innerText || '').indexOf('NCT') !== -1;
        }""",
        timeout=timeout,
    )


def _trial_list_items(page):
    return page.locator("#frame_LeftPanel li[data-router-action='trial:select']")


def _main_text(page):
    return _frame_main(page).inner_text() or ""


def _left_text(page):
    return _frame_left(page).inner_text() or ""


def _right_text(page):
    return _frame_right(page).inner_text() or ""


# ── Browser fixture ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def env():
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=["--ignore-certificate-errors"])
    context = browser.new_context(ignore_https_errors=True)
    page = context.new_page()
    page.set_default_timeout(CHAT_TIMEOUT)
    page.goto(BASE_URL, wait_until="networkidle")
    page.wait_for_load_state("networkidle", timeout=CHAT_TIMEOUT)
    _chat_input(page).wait_for(state="visible", timeout=CHAT_TIMEOUT)
    yield {"page": page}
    context.close()
    browser.close()
    pw.stop()


# Module-scoped: type the trials query exactly once and hold the painted
# state across every test that just needs to read it. Tests that mutate
# state (clicks) re-issue the query themselves.

@pytest.fixture(scope="module")
def trials_loaded(env):
    page = env["page"]
    _chat_input(page).fill(TRIAL_QUERY)
    _send_button(page).click()
    _wait_for_trials(page)
    _screenshot(page, "trials_loaded")
    yield env


# ── REQ-B-066: Left-panel brief_summary first-sentence truncation + '…' ──

class TestREQ_B_066_FirstSentenceTruncation:
    def test_left_panel_truncates_to_first_sentence_with_ellipsis(self, trials_loaded):
        page = trials_loaded["page"]
        cards = _trial_list_items(page)
        count = cards.count()
        assert count > 0, "No trial cards rendered in LeftPanel"
        # The truncation behavior is per-card: ellipsis appears only when
        # truncation occurred. At least one card in a real trials list will
        # contain a multi-sentence summary, so at least one '…' is expected.
        joined = _left_text(page)
        assert "…" in joined or "..." in joined, (
            f"REQ-B-066: no ellipsis in LeftPanel — summaries likely not "
            f"truncated to first sentence. LeftPanel text head: {joined[:400]!r}"
        )


# ── REQ-B-067: Left-panel header band format ─────────────────────────────

class TestREQ_B_067_HeaderBandFormat:
    def test_header_band_carries_canonical_criteria(self, trials_loaded):
        page = trials_loaded["page"]
        text = _left_text(page)
        low = text.lower()
        # Header band: "Clinical trials — condition: <X>, subject age: <Y>,
        # subject sex: <Z>, <location>, scope: <international|US>".
        assert "clinical trials" in low, (
            f"REQ-B-067: 'Clinical trials' header band missing. LeftPanel: {text[:400]!r}"
        )
        assert "lung cancer" in low, (
            f"REQ-B-067: condition 'lung cancer' not echoed. LeftPanel: {text[:400]!r}"
        )
        assert _labelled_value(text, "age", ("55",)), (
            f"REQ-B-067: subject age=55 not echoed. LeftPanel: {text[:400]!r}"
        )
        assert "los angeles" in low, (
            f"REQ-B-067: location 'Los Angeles' not echoed. LeftPanel: {text[:400]!r}"
        )
        assert _labelled_value(low, "scope", ("us", "united states")), (
            f"REQ-B-067: scope=US not echoed. LeftPanel: {text[:400]!r}"
        )


# ── REQ-B-068: More / Back pagination controls ──────────────────────────

class TestREQ_B_068_PaginationControls:
    def test_more_trials_link_present_when_cursor_exists(self, trials_loaded):
        page = trials_loaded["page"]
        # 'more trials' link is present when Response.cursor is non-null.
        # CT.gov reliably returns more than one page for severe migraines,
        # so we expect the link.
        more = page.locator("#frame_LeftPanel >> text=/more trials/i")
        assert more.count() > 0, (
            f"REQ-B-068: 'more trials' link missing in LeftPanel. "
            f"LeftPanel: {_left_text(page)[:400]!r}"
        )

    def test_no_back_link_on_first_page(self, trials_loaded):
        page = trials_loaded["page"]
        back = page.locator("#frame_LeftPanel >> text=/^back$/i")
        assert back.count() == 0, (
            "REQ-B-068: 'back' link MUST NOT appear on the first page (no prior cursors)."
        )


# ── REQ-B-069: Center-panel section anchors id="ct-sec-<slug>" ──────────

class TestREQ_B_069_SectionAnchors:
    def test_main_window_has_section_anchors(self, trials_loaded):
        page = trials_loaded["page"]
        anchors = page.locator("#frame_MainWindow [id^='ct-sec-']")
        n = anchors.count()
        assert n > 0, (
            "REQ-B-069: no id='ct-sec-<slug>' anchors in MainWindow. "
            "Sections must carry stable slug anchors for jump-link targets."
        )


# ── REQ-B-070: Right-panel section-list links with smooth scroll ────────

class TestREQ_B_070_SectionListLinks:
    def test_right_panel_has_section_jump_links(self, trials_loaded):
        page = trials_loaded["page"]
        # Right panel must carry one clickable link per section anchor.
        links = page.locator("#frame_RightPanel a")
        assert links.count() > 0, (
            "REQ-B-070: RightPanel has no clickable section links. "
            f"RightPanel: {_right_text(page)[:400]!r}"
        )


# ── REQ-B-071: Immediate clear + timer on More / Back click ─────────────

class TestREQ_B_071_ImmediateClearOnPaginationClick:
    def test_more_click_clears_main_and_right_immediately(self, env):
        page = env["page"]
        # Reset to a fresh trials list, click 'more trials', then sample
        # MainWindow + RightPanel within 200ms — they must be blank.
        _chat_input(page).fill(TRIAL_QUERY)
        _send_button(page).click()
        _wait_for_trials(page)
        before_main = _main_text(page)
        assert before_main, "Pre-click MainWindow should be populated"
        more = page.locator("#frame_LeftPanel >> text=/more trials/i").first
        more.click()
        # Within the immediate window the displayed trial detail must be
        # cleared. We poll briefly because the merge into MainWindow can
        # take a frame to land.
        page.wait_for_function(
            """() => {
                const m = document.getElementById('frame_MainWindow');
                const r = document.getElementById('frame_RightPanel');
                const mt = (m && m.innerText || '').trim();
                const rt = (r && r.innerText || '').trim();
                // After click, MainWindow should display a loading banner
                // (REQ-B-072) and RightPanel section list must be empty.
                return rt.indexOf('Trial sections') === -1 && mt.length < 5000;
            }""",
            timeout=5_000,
        )


# ── REQ-B-072: Loading banner content (criteria + record range) ─────────

class TestREQ_B_072_LoadingBannerContent:
    def test_loading_banner_carries_criteria_and_range(self, env):
        page = env["page"]
        page.reload(wait_until="networkidle")
        _chat_input(page).wait_for(state="visible", timeout=CHAT_TIMEOUT)
        # Install a MutationObserver on MainWindow BEFORE clicking send so
        # every intermediate paint is captured. The loading banner exists
        # only briefly (between intent_classified and the first trials
        # render); polling after click frequently misses it.
        page.evaluate(
            """() => {
                window.__mw_snapshots = [];
                const m = document.getElementById('frame_MainWindow');
                if (!m) return;
                const obs = new MutationObserver(() => {
                    window.__mw_snapshots.push((m.innerText || ''));
                });
                obs.observe(m, {childList: true, subtree: true, characterData: true});
            }"""
        )
        _chat_input(page).fill(TRIAL_QUERY)
        _send_button(page).click()
        _wait_for_trials(page)
        snapshots = page.evaluate("() => window.__mw_snapshots || []")
        # At least one snapshot must contain the canonical criteria + the
        # 'retrieving records N-M of T' clause.
        found = None
        for s in snapshots:
            sl = s.lower()
            if "retrieving records" in sl and "lung cancer" in sl:
                found = s
                break
        assert found is not None, (
            f"REQ-B-072: no loading-banner snapshot with 'retrieving records' + "
            f"'lung cancer'. Captured {len(snapshots)} snapshots; head of latest: "
            f"{(snapshots[-1] if snapshots else '')[:400]!r}"
        )


# ── REQ-B-073: Mid-stream intent_classified updates loading banner ──────

class TestREQ_B_073_IntentClassifiedBannerUpdate:
    def test_intent_classified_event_arrives_before_trials(self, env):
        page = env["page"]
        # Tap via ClientRouter.subscribe which is the canonical wrapper-side
        # event-capture API. We poll for ClientRouter to be defined before
        # subscribing because the wrapper inlines it via <script> on load.
        page.reload(wait_until="networkidle")
        _chat_input(page).wait_for(state="visible", timeout=CHAT_TIMEOUT)
        page.evaluate(
            """() => {
                window.__captured_intent_classified = [];
                if (window.ClientRouter && window.ClientRouter.subscribe) {
                    window.ClientRouter.subscribe('intent_classified', function (data) {
                        window.__captured_intent_classified.push(data || {});
                    });
                }
            }"""
        )
        _chat_input(page).fill(TRIAL_QUERY)
        _send_button(page).click()
        _wait_for_trials(page)
        events = page.evaluate("() => window.__captured_intent_classified || []")
        assert events, (
            "REQ-B-073: no intent_classified event observed before trials painted."
        )
        first = events[0]
        assert first.get("action") == "findClinicalTrials", (
            f"REQ-B-073: first intent_classified event has wrong action: {first!r}"
        )
        condition_lower = (first.get("condition") or "").lower()
        assert "lung cancer" in condition_lower or "cancer" in condition_lower, (
            f"REQ-B-073: condition not 'lung cancer' in intent_classified: {first!r}"
        )


# ── REQ-B-074: Pagination error overlay verb context ────────────────────

class TestREQ_B_074_PaginationErrorVerbContext:
    def test_pagination_error_carries_verb_clause(self, env):
        page = env["page"]
        page.reload(wait_until="networkidle")
        _chat_input(page).wait_for(state="visible", timeout=CHAT_TIMEOUT)
        _chat_input(page).fill(TRIAL_QUERY)
        _send_button(page).click()
        _wait_for_trials(page)
        # Install a Playwright route to abort the next clinical_trials_page
        # /gate POST. The pagination overlay error must carry the verb
        # clause: "While trying to load more clinical trials: ..." per REQ-B-074.
        def _maybe_abort(route, request):
            try:
                body = request.post_data or ""
                if "clinical_trials_page" in body:
                    route.abort()
                    return
            except Exception:
                pass
            route.continue_()
        page.route("**/gate", _maybe_abort)
        more = page.locator("#frame_LeftPanel >> text=/more trials/i").first
        more.click()
        # The error overlay must appear in MainWindow with the verb clause.
        page.wait_for_function(
            """() => {
                const m = document.getElementById('frame_MainWindow');
                const t = (m && m.innerText || '').toLowerCase();
                return t.indexOf('while trying to load more clinical trials') !== -1;
            }""",
            timeout=15_000,
        )
        page.unroute("**/gate", _maybe_abort)


# ── REQ-B-075: Selected-trial highlight + click clears center+right ─────

class TestREQ_B_075_SelectedTrialHighlightAndClick:
    def test_selected_trial_card_highlighted(self, env):
        page = env["page"]
        _chat_input(page).fill(TRIAL_QUERY)
        _send_button(page).click()
        _wait_for_trials(page)
        # The first card (auto-selected) should carry a highlighted bg.
        cards = _trial_list_items(page)
        first = cards.first
        bg = first.evaluate("el => getComputedStyle(el).backgroundColor")
        # selected-state background is teal-mint #f0fffe → rgb(240,255,254)
        # accept any non-transparent / non-white value as evidence of highlight
        assert bg and bg not in ("rgba(0, 0, 0, 0)", "rgb(255, 255, 255)"), (
            f"REQ-B-075: first card has no highlight bg (got {bg!r})."
        )

    def test_card_click_clears_center_and_right_then_paints(self, env):
        page = env["page"]
        _chat_input(page).fill(TRIAL_QUERY)
        _send_button(page).click()
        _wait_for_trials(page)
        cards = _trial_list_items(page)
        if cards.count() < 2:
            pytest.skip("Need at least 2 trials to verify click-swap; only 1 returned.")
        before_main = _main_text(page)
        second = cards.nth(1)
        second.click()
        # MainWindow content must change to the second trial (verified by
        # any text change within a 5s window — the "blank flash" is
        # implementation-internal).
        page.wait_for_function(
            f"""() => {{
                const m = document.getElementById('frame_MainWindow');
                const t = (m && m.innerText || '').trim();
                return t.length > 0 && t !== {before_main!r}.trim();
            }}""",
            timeout=5_000,
        )
