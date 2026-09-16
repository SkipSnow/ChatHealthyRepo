# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# find_care_transcript_test.py — proves the single conversation transcript
# at runtime, against the DOM the person is looking at.
#
# The acceptance invariant: there is EXACTLY ONE conversation record on the
# screen, and a user-submitted message appends to that one record. These
# tests assert that against https://localhost after a real submit — the
# transcript surface is exactly one element, and the person's own words show
# up in it as a user-attributed turn.
#
# DOM facts (measured, same as the sibling UAT suites): the frames are DIV /
# ASIDE / HEADER elements carrying id="frame_*" in the TOP document. The
# transcript is painted into #frame_UserMessage by the React TranscriptWidget
# via ClientRouter; its scroll region is #ch_transcript and each turn carries
# data-testid="transcript-turn" with data-speaker in {user, machine}.

import os

import pytest
from playwright.sync_api import sync_playwright

BASE_URL = os.getenv("SMOKE_TEST_URL", "https://localhost")
READY_TIMEOUT = 60_000
TURN_TIMEOUT = 90_000

PROMPT = "#frame_UserPromptAndControl input"
TRANSCRIPT = "#ch_transcript"
SURFACE = "[data-testid='conversation-transcript']"
USER_TURN = "#ch_transcript [data-testid='transcript-turn'][data-speaker='user']"
MACHINE_TURN = "#ch_transcript [data-testid='transcript-turn'][data-speaker='machine']"

MESSAGE = "find me a cardiologist in Boston Massachusetts"


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # localhost is served with a self-signed cert in the local stack.
        context = browser.new_context(ignore_https_errors=True)
        pg = context.new_page()
        pg.goto(BASE_URL, wait_until="domcontentloaded", timeout=READY_TIMEOUT)
        pg.locator(PROMPT).wait_for(state="visible", timeout=READY_TIMEOUT)
        # The transcript surface is laid down on load, before any turn.
        pg.locator(TRANSCRIPT).wait_for(state="attached", timeout=READY_TIMEOUT)
        yield pg
        context.close()
        browser.close()


def _surface_count(page) -> int:
    return page.evaluate(
        f"() => document.querySelectorAll(\"{SURFACE}\").length")


def test_exactly_one_conversation_surface_on_load(page):
    """The invariant's first half: one conversation record, not several."""
    assert _surface_count(page) == 1, (
        f"expected exactly one transcript surface, found {_surface_count(page)}")


def test_user_message_appends_to_the_single_transcript(page):
    """The invariant's second half: a user-submitted message appends to the
    one-and-only transcript, attributed to the user."""
    page.locator(PROMPT).fill(MESSAGE)
    page.locator(PROMPT).press("Enter")

    # The agent pushes the conversation record the moment the person's words
    # are persisted; the user turn shows before the model has answered.
    page.wait_for_function(
        "(msg) => {"
        " const els = document.querySelectorAll("
        "   \"#ch_transcript [data-testid='transcript-turn'][data-speaker='user']\");"
        " for (const el of els) {"
        "   if ((el.innerText || '').toLowerCase().includes(msg)) return true;"
        " }"
        " return false;"
        "}",
        arg="cardiologist",
        timeout=TURN_TIMEOUT,
    )

    # Still exactly one surface after the turn — nothing forked a second one.
    assert _surface_count(page) == 1, (
        f"a second conversation surface appeared: {_surface_count(page)}")

    user_text = page.locator(USER_TURN).last.inner_text().lower()
    assert "cardiologist" in user_text, (
        f"the user's words are not in the user turn: {user_text[:200]!r}")


def test_model_prose_appends_as_a_machine_turn(page):
    """The model's PROSE reply appends to the same one record, attributed to
    the machine — so both speakers share the single surface. A clean provider
    search answers with structured results in the main window and no prose, so
    this uses a message that elicits a clarification, which is when the model
    speaks a conversational turn."""
    page.locator(PROMPT).fill("hello, what can you help me with?")
    page.locator(PROMPT).press("Enter")
    page.wait_for_function(
        "() => document.querySelectorAll("
        " \"#ch_transcript [data-testid='transcript-turn'][data-speaker='machine']\""
        ").length >= 1",
        timeout=TURN_TIMEOUT,
    )
    assert page.locator(MACHINE_TURN).count() >= 1
    # Both a user turn and a machine turn now live in the one surface.
    assert page.locator(USER_TURN).count() >= 1
    assert _surface_count(page) == 1
