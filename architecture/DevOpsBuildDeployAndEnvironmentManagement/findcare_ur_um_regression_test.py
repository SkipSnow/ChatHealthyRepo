# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""ONE end-to-end regression file for every FindCare UR/UM use case.

This is NOT part of `find_care_smoke_test.py` (the smoke test) — it is
the sibling Playwright regression that drives the actual browser through
each UR/UM scenario and asserts on user-visible outputs. New UR/UM use
cases land HERE as additional test methods, not in their own sibling
files (operator's standing rule).

Use cases currently covered
---------------------------

1. **Geography disambiguation** —
   Turn 1: "I need a nurse practitioner to help me with my pain in
   milwaukee" — verbatim utterance reaches SpecialtyFilter so the role-
   scoring prompt honours "nurse practitioner"; UM emits kind:"prompt"
   ("Did you mean Milwaukee, Wisconsin?") mid-stream, the chat iframe
   shows it inline while the screen timer keeps running and Send is
   locked.
   Turn 2: "yes" — UM resolves the prior IntentDocument's
   pending_disambiguation, upgrades target_action to findAProvider, UR
   dispatches ProviderSearch, every returned provider address contains
   MILWAUKEE, WI.

2. **safetyLockout three-task flow** —
   Task C: a fresh-session utterance signalling immediate medical
   attention triggers UM to emit target_action=safetyLockout;
   LockoutTool inserts a {env}_Safety.emergency_incidents row and
   renders the "when you said '...'" + Skip phone-number text.
   Task B: an arbitrary utterance on the now-locked session — UR's pre-
   UM hydration short-circuits UM entirely and dispatches LockoutTool,
   which renders the "you are still safety-locked" reminder using the
   original trigger utterance (not the user's current one).
   Task A: the literal `"unlock$123"` (case-insensitive, strip+lower
   exact match) — LockoutTool writes unlocked=true to the
   emergency_incidents row, clears the in-memory lockout, renders the
   "you have been unlocked" success text. A normal request after that
   routes through UM normally.

The browser "stream closed" signal is the screen timer (data-testid
"prompt-row-timer") disappearing — that element renders only while
phase === 'searching' || reclassifying, and phase only leaves
'searching' on kind:"final" (per the race-prevention fix). Polling that
element's absence is the reliable way to know the prior /gate hit fully
completed before sending the next one.

Run standalone:
  python -m pytest architecture/DevOpsBuildDeployAndEnvironmentManagement/findcare_ur_um_regression_test.py -v

Requires:
  - The local stack up at https://localhost (run `python
    architecture/DevOpsBuildDeployAndEnvironmentManagement/deploy_chathealthy.py
    --env local` first; build the package first via
    `build_chathealthy.py --env local`).
"""
from __future__ import annotations

import os
import time
import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / '.git').exists():
        _lib = _d / 'FrontEndApplicationLib' / 'src'
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_frontend_lib.exceptions import ChatHealthyException

import pytest
from playwright.sync_api import expect, sync_playwright


BASE_URL = os.environ.get("FINDCARE_REGRESSION_URL", "https://localhost")

UTTERANCE_TURN_1 = "I need a nurse practitioner to help me with my pain in milwaukee"
UTTERANCE_TURN_2 = "yes"

# Generous because turn 1 chains UM (LLM) + SpecialtyFilter (LLM) and
# turn 2 chains UM (LLM) + ProviderSearch (DB). Each LLM hop can run 3–6s.
STREAM_CLOSED_TIMEOUT_MS = 60_000


@pytest.fixture(scope="module")
def page_ctx():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            ignore_https_errors=True,
        )
        page = context.new_page()
        yield {"page": page, "browser": browser, "context": context}
        context.close()
        browser.close()


def _chat_frame(page):
    """Return the FindCare chat iframe (the React app at :7860)."""
    deadline = time.time() + 30
    while time.time() < deadline:
        for f in page.frames:
            if ":7860" in f.url or "hf.space" in f.url:
                return f
        page.wait_for_timeout(250)
    raise ChatHealthyException(
        mode="assertion_failed",
        component="findcare_ur_um_regression_test",
        message=f"Chat iframe not found. Frames: {[f.url for f in page.frames]}")


def _wait_for_stream_closed(chat_frame, timeout_ms: int = STREAM_CLOSED_TIMEOUT_MS) -> None:
    """Poll until the screen timer disappears.

    The timer element renders only while the chat iframe is in
    phase='searching' (or reclassifying). The race-prevention fix in
    FindCareApp.tsx guarantees phase only leaves 'searching' on
    kind:"final" — so the timer disappearing is the reliable signal that
    the server-side pipeline (UM + downstream tools + persist) has fully
    completed and Send is safe to fire again.
    """
    timer = chat_frame.locator('[data-testid="prompt-row-timer"]')
    # First confirm the timer ever appeared — guards against a turn that
    # never started.
    timer.first.wait_for(state="visible", timeout=10_000)
    # Then wait for it to detach from the DOM (or become hidden).
    timer.first.wait_for(state="hidden", timeout=timeout_ms)


def _send(chat_frame, text: str) -> None:
    chat_input = chat_frame.locator("input[placeholder*='Type a message'], textarea").first
    chat_input.wait_for(state="visible", timeout=15_000)
    # Send button is gated until phase != 'searching' (or 'Wait…' during
    # mid-stream prompt). The disambiguation regression asserts this is
    # honored — typing should be allowed (input unlocked mid-stream) but
    # the form must not fire until Send is enabled.
    expect(chat_frame.locator("button", has_text="Send")).to_be_visible()
    chat_input.click()
    chat_input.fill(text)
    chat_frame.locator("button", has_text="Send").first.click()


def test_geography_disambiguation_e2e(page_ctx):
    page = page_ctx["page"]

    page.goto(BASE_URL, wait_until="load", timeout=60_000)
    chat_frame = _chat_frame(page)

    # ── Turn 1: city only — exercise role-name preservation. ────────────
    _send(chat_frame, UTTERANCE_TURN_1)
    _wait_for_stream_closed(chat_frame)

    # The parent-page filter panel renders the SpecialtyFilter output.
    # Nurse Practitioner rows MUST appear; pain-doctor-only output is a
    # regression on the SpecialtyFilter-input-restoration commit.
    filter_panel_text = ""
    filter_frame_loc = page.frame_locator('iframe[data-filter-frame]')
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            filter_panel_text = filter_frame_loc.locator("body").inner_text(timeout=2_000)
            if "Nurse Practitioner" in filter_panel_text:
                break
        except Exception:
            pass
        page.wait_for_timeout(500)
    assert "Nurse Practitioner" in filter_panel_text, (
        "REGRESSION: filter panel did not render any Nurse Practitioner "
        "specialty for the verbatim utterance "
        f"'{UTTERANCE_TURN_1}'. UR must pass the raw person utterance "
        "to SpecialtyFilter so the role-scoring prompt can honor the "
        "user-named role at 1.0. Filter panel text (first 600c): "
        f"{filter_panel_text[:600]}"
    )

    # The mid-stream system prompt should be visible to the user too —
    # exact phrasing is LLM-authored, but it must mention either
    # 'Milwaukee' or 'Wisconsin'.
    chat_body_t1 = chat_frame.locator("body").inner_text()
    assert ("Milwaukee" in chat_body_t1) or ("Wisconsin" in chat_body_t1), (
        "Turn 1 chat body missing the disambiguation prompt. "
        f"Chat body (first 600c): {chat_body_t1[:600]}"
    )

    # ── Turn 2: 'yes' resolves the pending state candidate. ─────────────
    _send(chat_frame, UTTERANCE_TURN_2)
    _wait_for_stream_closed(chat_frame)

    # Provider list MUST render and every visible provider address MUST
    # contain MILWAUKEE, WI. If the FE clobbered phase='results' with
    # 'clarify' (the prior bug), no providers render at all.
    deadline = time.time() + 15
    chat_body_t2 = ""
    while time.time() < deadline:
        chat_body_t2 = chat_frame.locator("body").inner_text()
        if "MILWAUKEE" in chat_body_t2.upper() and "NPI" in chat_body_t2.upper():
            break
        page.wait_for_timeout(500)
    assert "NPI" in chat_body_t2.upper(), (
        "REGRESSION: no providers rendered in the chat iframe after the "
        "disambiguation resolved. The FE final-handler used to clobber "
        "phase='results' with 'clarify' when sawPrompt was true; the "
        "fix gates that fall-back on !sawProviders. Chat body (first "
        f"600c): {chat_body_t2[:600]}"
    )
    # Every address line that contains a state abbreviation must say WI,
    # and Milwaukee must appear. We don't assert ALL providers because
    # the iframe may render only the first window; the assertion is
    # "what is rendered is consistent with the Milwaukee scope."
    upper_body = chat_body_t2.upper()
    assert "MILWAUKEE" in upper_body, (
        "Provider list rendered but no address contains MILWAUKEE — "
        "ProviderSearch geography did not apply. Chat body (first 600c): "
        f"{chat_body_t2[:600]}"
    )
    # Defence-in-depth: there should not be a Delaware or other-state
    # provider visible in the rendered list since geography was scoped.
    for forbidden_state in (", DE,", ", DE ", ", IL,", ", IA,", ", MN,"):
        assert forbidden_state not in upper_body.replace(", WI ", "").replace(", WI,", ""), (
            f"Provider list contains an out-of-scope state '{forbidden_state.strip(', ')}' — "
            "ProviderSearch did not apply the resolved Wisconsin scope. "
            f"Chat body (first 600c): {chat_body_t2[:600]}"
        )


# ────────────────────────────────────────────────────────────────────
# safetyLockout — three-task end-to-end
# ────────────────────────────────────────────────────────────────────

SAFETY_TRIGGER = (
    "I am having crushing chest pain right now and I cannot breathe"
)
SAFETY_UNLOCK_LITERAL = "unlock$123"
SAFETY_ARBITRARY = "are you there?"


def test_safety_lockout_three_tasks_e2e(page_ctx):
    """Task C → Task B → Task A, all in one browser session.

    UM-emitted lockout (Task C), UR pre-UM hydration short-circuit
    (Task B), unlock literal (Task A), then a normal post-unlock
    request — every turn polled on the screen timer just like the
    disambiguation flow above.
    """
    page = page_ctx["page"]

    page.goto(BASE_URL, wait_until="load", timeout=60_000)
    chat_frame = _chat_frame(page)

    # ── Task C: fresh-session immediate-medical-attention utterance. ────
    _send(chat_frame, SAFETY_TRIGGER)
    _wait_for_stream_closed(chat_frame)

    chat_body_c = chat_frame.locator("body").inner_text()
    assert SAFETY_TRIGGER in chat_body_c, (
        "Task C system message must include the verbatim trigger so the "
        "user reads 'when you said \"...\"'. Chat body (first 800c): "
        f"{chat_body_c[:800]}"
    )
    assert "1 hour" in chat_body_c, (
        "Task C message must state the 1-hour lockout duration. "
        f"Chat body (first 800c): {chat_body_c[:800]}"
    )
    assert "Skip" in chat_body_c and "646 408 8999" in chat_body_c, (
        "Task C message must offer operator support: "
        "'contact Skip at 646 408 8999'. Chat body (first 800c): "
        f"{chat_body_c[:800]}"
    )

    # ── Task B: arbitrary utterance on locked session — UR short-circuits.
    _send(chat_frame, SAFETY_ARBITRARY)
    _wait_for_stream_closed(chat_frame)

    chat_body_b = chat_frame.locator("body").inner_text()
    assert "still safety-locked" in chat_body_b, (
        "Task B reminder must say 'still safety-locked'. "
        f"Chat body (first 800c): {chat_body_b[:800]}"
    )
    assert SAFETY_TRIGGER in chat_body_b, (
        "Task B reminder must include the ORIGINAL trigger utterance "
        "(not the user's current utterance). The Lockout sub-object "
        "on user_object preserves it across turns. Chat body (first "
        f"800c): {chat_body_b[:800]}"
    )

    # ── Task A: the literal '$unlock' — case-insensitive exact match.
    _send(chat_frame, SAFETY_UNLOCK_LITERAL)
    _wait_for_stream_closed(chat_frame)

    chat_body_a = chat_frame.locator("body").inner_text()
    assert "unlocked" in chat_body_a.lower(), (
        "Task A success message must announce the unlock. "
        f"Chat body (first 800c): {chat_body_a[:800]}"
    )
    assert "911" in chat_body_a, (
        "Task A success message must remind the user to call 911 in a "
        f"real emergency. Chat body (first 800c): {chat_body_a[:800]}"
    )

    # ── Post-unlock: a normal request must route through UM normally. ───
    _send(chat_frame, "Find me a bone doctor in Delaware")
    _wait_for_stream_closed(chat_frame)

    chat_body_post = chat_frame.locator("body").inner_text()
    assert "NPI" in chat_body_post.upper() or "Orthopaedic" in chat_body_post or "specialty" in chat_body_post.lower(), (
        "After Task A unlocks the session, normal provider-search "
        "routing must resume — providers or specialties should render. "
        f"Chat body (first 800c): {chat_body_post[:800]}"
    )
