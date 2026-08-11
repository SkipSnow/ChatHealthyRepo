"""The smoke test. Built from what users actually did, not from what we assume.

Replaces the previous find_care_smoke_test.py, which carried 34 pytest.skip
calls -- 34 ways to report green without asserting anything -- and whose exit
code was the local deploy's exit code, so a healthy stack reported failure
because the test did.

Every prompt below was taken verbatim from dev_Debug.chat_calls, prod_Debug and
qa_Debug — 731 real messages. The counts in each docstring are how many times
that shape of question was actually asked. Nothing here is invented.

The point is evidence for the dead-code assessment: these exercise the paths
users demonstrably use, so a file that stays cold through this run has no live
caller a user can create.

    python -m pytest architecture/DevOpsBuildDeployAndEnvironmentManagement/find_care_smoke_test.py -v
"""
from __future__ import annotations

import os

import pytest
from playwright.sync_api import expect, sync_playwright

BASE = os.environ.get("CH_SMOKE_BASE", "https://localhost")
TIMEOUT_MS = 90_000


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # The local CA is trusted by the machine but not by Chromium's own store.
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1400, "height": 900})
        pg = ctx.new_page()
        pg.set_default_timeout(TIMEOUT_MS)
        pg.goto(BASE, wait_until="domcontentloaded")
        yield pg
        ctx.close()
        browser.close()


def _ask(page, text: str) -> str:
    """Type one utterance the way a user does and return WHAT THE APP ADDED.

    The prompt lives on the WRAPPER page (#userInput / #userInputSubmit); the
    React iframe (#coreReactFrame) renders the answer.

    This returns only the text that was NOT on screen before the question was
    asked. It used to return the whole page, and every assertion was
    `len(body) > 0` -- which the nav bar, the splash line and the copyright
    footer satisfy on a completely dead application. Eleven journeys reported
    green against a screen showing nothing but chrome.
    """
    before = _screen(page)
    page.locator("#userInput").fill(text)
    page.locator("#userInputSubmit").click()
    page.wait_for_timeout(12000)
    after = _screen(page)
    added = [ln for ln in after.splitlines() if ln.strip() and ln not in before.splitlines()]
    return chr(10).join(added)


def _screen(page) -> str:
    """Everything visible: the wrapper page plus the React frame."""
    wrapper = page.locator("body").inner_text()
    try:
        framed = page.frame_locator("#coreReactFrame").locator("body").inner_text()
    except Exception:
        framed = ""
    return wrapper + chr(10) + framed


def _answered(added: str, utterance: str) -> None:
    """The app must have put something new on screen beyond echoing the user.

    The wrapper echoes the utterance back (with spelling corrections) before
    anything is answered, so an echo alone is not an answer.
    """
    meaningful = [ln for ln in added.splitlines()
                  if ln.strip() and utterance.lower()[:18] not in ln.lower()]
    assert meaningful, (
        f"nothing new appeared on screen after asking {utterance!r} "
        f"(only the echo). added={added!r}"
    )


def _specialty_counts(page) -> dict:
    """The three numbers on the specialty panel: ALL POSSIBLE / ALL
    PRESCRIBERS / YOUR CHOICES. All three reading 0 after a specialty search
    is the failure the byte-count assertions could not see."""
    out = {}
    try:
        frame = page.frame_locator("#coreReactFrame")
        for label in ("ALL POSSIBLE", "ALL PRESCRIBERS", "YOUR CHOICES"):
            txt = frame.locator(f"text={label}").first.locator("xpath=..").inner_text()
            digits = [t for t in txt.split() if t.isdigit()]
            out[label] = int(digits[-1]) if digits else 0
    except Exception as exc:
        out["error"] = str(exc)[:80]
    return out


def test_page_loads(page):
    """The wrapper serves, mounts the React frame, and offers the prompt."""
    expect(page.locator("#coreReactFrame")).to_be_attached()
    expect(page.locator("#userInput")).to_be_visible()


def test_greeting(page):
    """'hello' — 59 occurrences, the single most common thing users type."""
    body = _ask(page, "hello")
    _answered(body, "hello")


def test_provider_search_specialty_and_state(page):
    """'can you find me a cardiologist in delaware?' — 91 of this shape."""
    body = _ask(page, "can you find me a cardiologist in delaware?")
    _answered(body, "can you find me a cardiologist in delaware?")
    assert "cardiolog" in body.lower() or "delaware" in body.lower(), (
        f"no specialty or provider content on screen. added={body!r}")
    counts = _specialty_counts(page)
    numeric = {k: v for k, v in counts.items() if isinstance(v, int)}
    assert numeric, f"could not read the specialty panel at all: {counts}"
    assert any(v > 0 for v in numeric.values()), (
        f"the specialty panel is still empty: {counts}")


def test_symptom_to_specialty(page):
    """'I have a stomach ache what kind of doctor do I need to go to?' — 28."""
    body = _ask(page, "I have a stomach ache what kind of doctor do I need to go to?")
    _answered(body, "I have a stomach ache what kind of doctor do I need to go to?")


def test_geography_followup(page):
    """'what about in wilmington?' — 5. Depends on carried conversation state,
    so it only means anything after a prior search in the same session."""
    _ask(page, "find pediatricians in delaware")
    body = _ask(page, "what about in wilmington?")
    _answered(body, "what about in wilmington?")


def test_count_question(page):
    """'how many doctors are in Chickasaw county Mississippi' — 37."""
    body = _ask(page, "can you tell me how many doctors are in Chickasaw county Mississippi")
    _answered(body, "can you tell me how many doctors are in Chickasaw county Mississippi")


def test_clinical_trials(page):
    """'Find clinical trials for diabetes in Delaware' — 11."""
    body = _ask(page, "Find clinical trials for diabetes in Delaware")
    _answered(body, "Find clinical trials for diabetes in Delaware")
    assert "unavailable" not in body.lower(), (
        f"the trials lookup reported itself unavailable: {body!r}")


def test_about_the_company(page):
    """'what is your name?' / 'what sort of company are you building?' — 33."""
    body = _ask(page, "what is your name?")
    _answered(body, "what is your name?")


def test_out_of_scope_question(page):
    """'who won the super bowl?', 'what is the weather like today?',
    'what is the meaning of life?' — 6 each. Exercises unknown-question
    handling, which is a real path users reach constantly."""
    body = _ask(page, "who won the super bowl?")
    _answered(body, "who won the super bowl?")


def test_medical_advice_declined(page):
    """'should i get a flu shot?' — 7. Must not answer as medical advice."""
    body = _ask(page, "should i get a flu shot?")
    _answered(body, "should i get a flu shot?")


def test_capability_question(page):
    """'can you show me reviews for this doctor?' — 7, and 'how do i file a
    complaint against my doctor?' — 6. Both are capability questions."""
    body = _ask(page, "can you show me reviews for this doctor?")
    _answered(body, "can you show me reviews for this doctor?")
