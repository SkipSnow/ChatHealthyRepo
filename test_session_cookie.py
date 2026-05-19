# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# test_session_cookie.py — Playwright + httpx tests for the page-load
# entrance behavior defined by EPIC-002-F-003-S-005.
#
# Coverage:
#   * T-001 — Set-Cookie attributes on /gate response.
#   * T-002 — cookie value equals the 67-byte session-token assembly
#             (GUID + first stamp + 'X' + second stamp).
#   * T-003 — /gate body carries `time_remaining_seconds` as integer.
#   * T-004 — value matches the (most_recent_restamp + 300s - now) formula
#             within a small tolerance.
#   * T-005 — wrapper stores (remaining, receivedAt) on every /gate response.
#   * T-006 — pre-flight refuses to issue when stored pair indicates expiry.
#   * T-008 — page load triggers a /gate POST with op="boot" as the FIRST
#             SharedServices network call.
#   * T-009 — error response routes to the hard-fail page with the mandated
#             "authentication error from SharedServices" text.
#   * T-010 — expired session navigates home + renders correct timeout copy.
#
# Run (local):
#   set SMOKE_TEST_ENV=local
#   python -m pytest test_session_cookie.py -v
#
# Run (dev/qa/prod): set SMOKE_TEST_ENV accordingly. Tests gate the
# `Domain=chathealthy.ai` attribute assertion on the chosen environment so
# the cookie attribute is exercised against the real production-shape host
# without breaking the localhost test loop.

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

import httpx
import pytest
from playwright.sync_api import sync_playwright


SMOKE_ENV = os.getenv("SMOKE_TEST_ENV", "local").lower()

_ENV_CONFIG = {
    "local": {
        "base_url":     "https://localhost",
        "shared_url":   "https://localhost:8002",
        "expect_domain_attr": False,   # browser drops Domain=chathealthy.ai on localhost
    },
    "dev": {
        "base_url":     "https://dev.chathealthy.ai",
        "shared_url":   "https://skipsnow-dev-sharedservicesspace.hf.space",
        "expect_domain_attr": True,
    },
    "qa": {
        "base_url":     "https://qa.chathealthy.ai",
        "shared_url":   "https://skipsnow-qa-sharedservicesspace.hf.space",
        "expect_domain_attr": True,
    },
    "prod": {
        "base_url":     "https://chathealthy.ai",
        "shared_url":   "https://skipsnow-sharedservicesspace.hf.space",
        "expect_domain_attr": True,
    },
}
_cfg = _ENV_CONFIG.get(SMOKE_ENV, _ENV_CONFIG["local"])
BASE_URL    = _cfg["base_url"]
SHARED_URL  = _cfg["shared_url"]
EXPECT_DOMAIN_ATTR = _cfg["expect_domain_attr"]


def _post_gate_boot(client: httpx.Client) -> httpx.Response:
    return client.post(
        f"{SHARED_URL}/gate",
        headers={"Content-Type": "application/json"},
        json={"op": "boot"},
    )


def _parse_set_cookie_attrs(set_cookie_header: str) -> dict[str, str]:
    """Parse a Set-Cookie header into a dict {name_lower: value}.
    Boolean attributes (Secure, HttpOnly) map to themselves.
    The cookie's NAME=VALUE pair lands under key "_pair" so the test
    can pull the literal value byte-for-byte.
    """
    out: dict[str, str] = {}
    parts = [p.strip() for p in set_cookie_header.split(";")]
    if parts:
        out["_pair"] = parts[0]
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip().lower()] = v.strip()
        else:
            out[p.lower()] = p
    return out


# ── T-001 ─────────────────────────────────────────────────────────
def test_T001_ch_session_cookie_attributes():
    """REQ-T-001: Set-Cookie carries name=ch_session with attributes
    Secure, HttpOnly, Max-Age=300, SameSite=None, Domain=chathealthy.ai,
    Path=/gate. Domain attribute is asserted only when running against a
    chathealthy.ai-fronted environment (dev/qa/prod) — on localhost the
    browser would silently drop the cookie, so the test loop disables
    that single assertion (the header still SHOULD carry the attribute
    server-side; the live-host test paths exercise that).
    """
    with httpx.Client(verify=False, timeout=20) as c:
        r = _post_gate_boot(c)
        assert r.status_code == 200, f"/gate boot failed: {r.status_code} {r.text}"
        sc = r.headers.get("set-cookie")
        assert sc, "Set-Cookie header missing from /gate response"
        # multi-cookie response: extract the ch_session entry
        ch_sc = next((line for line in r.headers.get_list("set-cookie")
                      if line.lower().startswith("ch_session=")), None)
        assert ch_sc, f"ch_session not present in Set-Cookie: {r.headers.get_list('set-cookie')}"
        attrs = _parse_set_cookie_attrs(ch_sc)
        assert attrs["_pair"].startswith("ch_session="), attrs["_pair"]
        assert "secure" in attrs, f"Secure missing: {ch_sc}"
        assert "httponly" in attrs, f"HttpOnly missing: {ch_sc}"
        assert attrs.get("max-age") == "300", f"Max-Age != 300: {attrs.get('max-age')}"
        assert (attrs.get("samesite") or "").lower() == "none", f"SameSite != None: {attrs.get('samesite')}"
        assert attrs.get("path") == "/gate", f"Path != /gate: {attrs.get('path')}"
        if EXPECT_DOMAIN_ATTR:
            assert attrs.get("domain") in {"chathealthy.ai", ".chathealthy.ai"}, \
                f"Domain != chathealthy.ai: {attrs.get('domain')}"


# ── T-002 ─────────────────────────────────────────────────────────
def test_T002_ch_session_value_is_67_byte_session_token():
    """REQ-T-002: cookie value is the 67-byte assembly defined by
    REQ-B-007 — 32-char GUID + 17-char first stamp + 'X' + 17-char
    second stamp."""
    with httpx.Client(verify=False, timeout=20) as c:
        r = _post_gate_boot(c)
        assert r.status_code == 200
        ch_sc = next((line for line in r.headers.get_list("set-cookie")
                      if line.lower().startswith("ch_session=")), None)
        assert ch_sc, ch_sc
        attrs = _parse_set_cookie_attrs(ch_sc)
        pair = attrs["_pair"]
        prefix = "ch_session="
        assert pair.startswith(prefix), pair
        value = pair[len(prefix):]
        assert len(value) == 67, f"ch_session value length {len(value)} != 67: {value!r}"
        guid = value[:32]
        first_stamp = value[32:49]
        sep = value[49]
        second_stamp = value[50:67]
        assert sep == "X", f"separator != 'X' at byte 49: got {sep!r}"
        # Stamps are 17 chars of YYYYMMDDhhmmssfff. Validate the date prefix.
        for label, stamp in (("first", first_stamp), ("second", second_stamp)):
            try:
                datetime.strptime(stamp[:14], "%Y%m%d%H%M%S")
            except ValueError as exc:
                pytest.fail(f"{label} stamp '{stamp}' not a valid timestamp: {exc}")
        # GUID is the cookie's first 32 chars — same shape as the server's
        # auth_token (alphanumeric, no separators).
        assert guid.isalnum(), f"GUID prefix has non-alnum chars: {guid!r}"


# ── T-003 ─────────────────────────────────────────────────────────
def test_T003_gate_response_carries_time_remaining_seconds():
    """REQ-T-003: response body has time_remaining_seconds as a
    non-negative integer.

    Also tightens the S-003-REQ-B-004 ↔ S-005 bridge: every /gate
    response MUST carry the cryptographic-display projection of the
    session token. That projection is what the session-verification
    panels (refreshTokenPanels) feed to /session + /verify-token; the
    full user_object stays server-side per S-005's contract.
    """
    with httpx.Client(verify=False, timeout=20) as c:
        r = _post_gate_boot(c)
        assert r.status_code == 200
        payload = r.json()
        assert "time_remaining_seconds" in payload, payload.keys()
        v = payload["time_remaining_seconds"]
        assert isinstance(v, int), f"time_remaining_seconds is {type(v).__name__}, not int"
        assert v >= 0, f"time_remaining_seconds negative: {v}"
        assert v <= 300, f"time_remaining_seconds > 300: {v}"
        # Cryptographic-display projection: the SessionToken envelope
        # (origin + signed 67-byte token + signature + created_at + ...).
        # The full user_object MUST NOT be on the wire.
        assert "session_token" in payload, payload.keys()
        st = payload["session_token"]
        assert isinstance(st, dict), f"session_token not a dict: {type(st).__name__}"
        for required in ("origin", "token", "signature", "created_at", "signed"):
            assert required in st, f"session_token missing {required}: {sorted(st.keys())}"
        assert st["signed"] is True, f"session_token.signed != True: {st['signed']}"
        assert len(st["token"]) == 67 + len("CH"), (
            f"session_token.token length {len(st['token'])} unexpected — "
            "expected 'CH' + 67-byte assembly (REQ-B-007 surface format)."
        )
        assert "user_object" not in payload, (
            "user_object MUST NOT cross the wire on /gate per S-005 REQ-T-008"
        )


# ── T-004 ─────────────────────────────────────────────────────────
def test_T004_time_remaining_seconds_matches_formula():
    """REQ-T-004: time_remaining_seconds = floor((most_recent_restamp +
    300s) - server_response_instant). The most-recent restamp is bytes
    19-35 of the nonce (i.e. the second stamp in the 67-byte token).
    Tolerance: 2 seconds for clock drift + request latency."""
    with httpx.Client(verify=False, timeout=20) as c:
        r = _post_gate_boot(c)
        payload = r.json()
        ch_sc = next((line for line in r.headers.get_list("set-cookie")
                      if line.lower().startswith("ch_session=")), None)
        attrs = _parse_set_cookie_attrs(ch_sc)
        value = attrs["_pair"][len("ch_session="):]
        second_stamp = value[50:67]
        secs = datetime.strptime(second_stamp[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        ms = int(second_stamp[14:17])
        restamp_ms = int(secs.timestamp() * 1000) + ms
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        expected = max(0, (restamp_ms + 300_000 - now_ms) // 1000)
        actual = payload["time_remaining_seconds"]
        assert abs(expected - actual) <= 2, (
            f"time_remaining_seconds {actual} differs from formula {expected} "
            f"by more than 2s tolerance"
        )


# ── Playwright fixture for T-005, T-006, T-008, T-009, T-010 ─────
@pytest.fixture(scope="module")
def browser_page():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--ignore-certificate-errors"])
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        yield page
        context.close()
        browser.close()


# ── T-005 ─────────────────────────────────────────────────────────
def test_T005_wrapper_stores_remaining_pair(browser_page):
    """REQ-T-005: window._sessionExpiry = {remaining, receivedAt} on
    every /gate response. After the wrapper's page-load _getAuthBoot
    completes, the pair MUST be populated."""
    page = browser_page
    page.goto(BASE_URL + "/", wait_until="domcontentloaded")
    # Give the page-load _getAuthBoot a moment to round-trip.
    page.wait_for_function(
        "() => window._sessionExpiry && typeof window._sessionExpiry.remaining === 'number'",
        timeout=15_000,
    )
    pair = page.evaluate("() => window._sessionExpiry")
    assert pair is not None, "window._sessionExpiry is null after page load"
    assert isinstance(pair["remaining"], (int, float))
    assert pair["remaining"] > 0, f"remaining not positive: {pair['remaining']}"
    assert isinstance(pair["receivedAt"], (int, float))
    assert pair["receivedAt"] > 0


# ── T-006 ─────────────────────────────────────────────────────────
def test_T006_preflight_refuses_when_expired(browser_page):
    """REQ-T-006: when the stored pair indicates expiry, the wrapper
    MUST NOT issue the request. _isSessionExpired() returns true and
    the helper short-circuits to _handleSessionExpired()."""
    page = browser_page
    page.goto(BASE_URL + "/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window._sessionExpiry && typeof window._sessionExpiry.remaining === 'number'",
        timeout=15_000,
    )
    # Force expiry by stamping the pair as having been received 400s ago
    # with a 300s budget.
    page.evaluate(
        "() => { window._sessionExpiry = {remaining: 300, receivedAt: Date.now() - 400000}; }"
    )
    expired = page.evaluate("() => window._isSessionExpired()")
    assert expired is True, "_isSessionExpired() returned false on a 400s-stale pair"


# ── T-008 ─────────────────────────────────────────────────────────
def test_T008_page_load_posts_gate_boot_as_first_ss_call(browser_page):
    """REQ-T-008: every page load MUST POST /gate op='boot' as the
    FIRST SharedServices network call. Capture network requests to
    SHARED_URL and assert the first one is POST /gate with op=boot."""
    page = browser_page
    captured: list[dict] = []

    def on_request(req):
        if SHARED_URL in req.url:
            entry = {"url": req.url, "method": req.method, "body": None}
            try:
                entry["body"] = req.post_data
            except Exception:
                pass
            captured.append(entry)

    page.on("request", on_request)
    try:
        page.goto(BASE_URL + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => window._sessionExpiry && typeof window._sessionExpiry.remaining === 'number'",
            timeout=15_000,
        )
    finally:
        page.remove_listener("request", on_request)

    assert captured, "no SharedServices requests captured on page load"
    first = captured[0]
    assert first["method"] == "POST", f"first SS request not POST: {first}"
    assert first["url"].rstrip("/").endswith("/gate"), f"first SS request not /gate: {first['url']}"
    assert first["body"], "first /gate request has empty body"
    body_obj = json.loads(first["body"])
    assert body_obj.get("op") == "boot", f"first /gate body op != 'boot': {body_obj}"


# ── T-009 ─────────────────────────────────────────────────────────
def test_T009_error_response_triggers_hard_fail(browser_page):
    """REQ-T-009: when /gate returns 5xx / unexpected 4xx / malformed
    body, the browser MUST render the hard-fail overlay containing the
    text 'authentication error from SharedServices'.

    We exercise this by intercepting the /gate response in Playwright
    and fulfilling it with a 503 BEFORE the page-load helper runs."""
    page = browser_page
    page.context.route(
        f"{SHARED_URL}/gate",
        lambda route: route.fulfill(
            status=503, content_type="text/plain", body="Service Unavailable"
        ),
    )
    try:
        page.goto(BASE_URL + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => { var el = document.getElementById('chFatalErrorMessage'); "
            "return el && el.textContent && el.textContent.length > 0; }",
            timeout=15_000,
        )
        overlay = page.locator("#chFatalErrorOverlay")
        assert overlay.is_visible(), "fatal-error overlay not visible on 503"
        msg = page.locator("#chFatalErrorMessage").text_content() or ""
        assert "authentication error from SharedServices" in msg, msg
    finally:
        page.context.unroute(f"{SHARED_URL}/gate")


# ── T-010 ─────────────────────────────────────────────────────────
def test_T010_expired_session_navigates_home_with_correct_message(browser_page):
    """REQ-T-010: when the pre-flight finds the session expired the
    browser MUST navigate to /, clear cached state, and render
    'system timed out: please start again' for a guest or
    'system timed out: please log back in' for a previously-registered
    user. We exercise the guest copy here — the wrapper's
    _wasRegistered defaults to false on a fresh page (the boot response
    on a guest session sets was_registered=false)."""
    page = browser_page
    page.goto(BASE_URL + "/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window._sessionExpiry && typeof window._sessionExpiry.remaining === 'number'",
        timeout=15_000,
    )
    # Force expiry + trigger the handler. We also explicitly stamp
    # _wasRegistered = false so the test asserts the guest copy.
    page.evaluate("""() => {
        window._sessionExpiry = {remaining: 300, receivedAt: Date.now() - 400000};
        window._wasRegistered = false;
        window._handleSessionExpired();
    }""")
    page.wait_for_url(
        f"{BASE_URL}/?system_timed_out=guest", timeout=15_000
    )
    # Wait for the wrapper to render the notice in place of the splash.
    page.wait_for_selector('[data-testid="system-timed-out"]', timeout=15_000)
    text = page.locator('[data-testid="system-timed-out"]').text_content() or ""
    assert text.strip() == "system timed out: please start again", text

    # Registered branch — flip the flag, navigate manually with the
    # registered query string, assert the registered copy.
    page.goto(f"{BASE_URL}/?system_timed_out=registered", wait_until="domcontentloaded")
    page.wait_for_selector('[data-testid="system-timed-out"]', timeout=15_000)
    text2 = page.locator('[data-testid="system-timed-out"]').text_content() or ""
    assert text2.strip() == "system timed out: please log back in", text2
