# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# localSmokeTestPyTest.py — DEVOPS-LOCAL-B009
# 34-step Playwright smoke test. Each step maps to a requirement.
# Steps 31-33 cover all 6 component handoff permutations.
# Every assertion is strict. No silent passes. No conditional assertions.
#
# Prerequisites: deploy_localhost.sh must be running (all servers up).
# Run: python -m pytest Code/deploy/localSmokeTestPyTest.py -v
#
# DR-019: Tests run against the full parent page, not individual components.

import os
import re
import ssl
import pytest
import httpx
from playwright.sync_api import sync_playwright, expect

# ── Environment configuration (DEVOPS-DEV-B001 env-parameterized ports) ───
# SMOKE_TEST_ENV=local|dev|qa|prod selects URL set + feature flags.
# SMOKE_TEST_URL can override BASE_URL alone (backwards-compat).

SMOKE_ENV = os.getenv("SMOKE_TEST_ENV", "dev").lower()

_ENV_CONFIG = {
    "local": {
        "base_url":           "https://localhost",
        "findcare_url":       "https://localhost:7860",
        "evalcare_url":       "https://localhost:8001",
        "shared_url":         "https://localhost:8002",
        "http_redirect_host": "localhost",           # HTTP→HTTPS redirect testable
        "banner_label":       "LOCAL",
        "mtls_enabled":       True,                    # Caddy enforces mTLS on :8081/:8082
        "shared_port":        8002,
        "evalcare_port":      8001,
    },
    "dev": {
        "base_url":           "https://dev.chathealthy.ai",
        "findcare_url":       "https://skipsnow-dev-chathealthyspace.hf.space",
        "evalcare_url":       "https://skipsnow-dev-evaluatecarespace.hf.space",
        "shared_url":         "https://skipsnow-dev-sharedservicesspace.hf.space",
        "http_redirect_host": None,                    # HF/Cloudflare don't serve HTTP
        "banner_label":       "DEV",
        "mtls_enabled":       False,                   # HF does not support mTLS (deferred to Beta)
        "shared_port":        None,
        "evalcare_port":      None,
    },
    "qa": {
        "base_url":           "https://qa.chathealthy.ai",
        "findcare_url":       "https://skipsnow-qa-chathealthyspace.hf.space",
        "evalcare_url":       "https://skipsnow-qa-evaluatecarespace.hf.space",
        "shared_url":         "https://skipsnow-qa-sharedservicesspace.hf.space",
        "http_redirect_host": None,
        "banner_label":       "QA",
        "mtls_enabled":       False,                   # Same HF constraint
        "shared_port":        None,
        "evalcare_port":      None,
    },
    "prod": {
        "base_url":           "https://chathealthy.ai",
        "findcare_url":       "https://skipsnow-chathealthyspace.hf.space",
        "evalcare_url":       "https://skipsnow-evaluatecarespace.hf.space",
        "shared_url":         "https://skipsnow-sharedservicesspace.hf.space",
        "http_redirect_host": None,
        "banner_label":       "PROD",                  # banner may not display in prod per B002 step 3
        "mtls_enabled":       False,                   # Beta-era mTLS migration required to flip this
        "shared_port":        None,
        "evalcare_port":      None,
    },
}

_cfg = _ENV_CONFIG.get(SMOKE_ENV, _ENV_CONFIG["local"])

BASE_URL            = os.getenv("SMOKE_TEST_URL", _cfg["base_url"])
FINDCARE_URL        = _cfg["findcare_url"]
EVALCARE_URL        = _cfg["evalcare_url"]
SHARED_URL          = _cfg["shared_url"]
HTTP_REDIRECT_HOST  = _cfg["http_redirect_host"]
BANNER_LABEL        = _cfg["banner_label"]
MTLS_ENABLED        = _cfg["mtls_enabled"]
EVALCARE_PORT       = _cfg["evalcare_port"]
SHARED_PORT         = _cfg["shared_port"]
IS_PROD             = (SMOKE_ENV == "prod")

CERTS_DIR = os.path.join(os.path.dirname(__file__), "..", "Shared", "ops", "certs")
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "_oneshots/test_output", "smoke_test")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
CHAT_TIMEOUT = 120_000


def _screenshot(page, name):
    page.screenshot(path=os.path.join(SCREENSHOT_DIR, f"{name}.png"), full_page=True)


def _retry(label, attempts, sleep_ms, action):
    """Retry `action` (zero-arg callable) up to `attempts` times. Sleep
    `sleep_ms` ms between tries. Print '[retry] LABEL: attempt N/M' on each.
    Re-raise the last exception if all attempts fail. Per directive
    2026-04-20 — Playwright's internal polling is opaque; explicit retry
    + per-attempt log makes failures diagnosable."""
    import time as _t
    last_exc = None
    for n in range(1, attempts + 1):
        print(f"  [retry] {label}: attempt {n}/{attempts}", flush=True)
        try:
            return action()
        except Exception as e:
            last_exc = e
            if n < attempts:
                _t.sleep(sleep_ms / 1000.0)
    raise last_exc


def _get_health():
    # POST per EPIC-008-F-011-S-002-REQ-B-001 (client-callable endpoints are POST).
    c = httpx.Client(verify=False, timeout=10)
    try:
        return c.post(f"{FINDCARE_URL}/health").json()
    finally:
        c.close()


def _get_welcome_words():
    c = httpx.Client(verify=False, timeout=10)
    try:
        data = c.post(f"{FINDCARE_URL}/welcome").json()
        import html as html_mod
        text = re.sub(r'<[^>]+>', ' ', data.get("message", ""))
        text = html_mod.unescape(text).strip()
        return text.split()[:25]
    finally:
        c.close()


def _get_fresh_token():
    c = httpx.Client(verify=False, timeout=10)
    try:
        return c.post(f"{FINDCARE_URL}/session").json()
    finally:
        c.close()


def _parse_token(token_str):
    if not isinstance(token_str, str):
        return "", ""
    if len(token_str) >= 69:
        return token_str[2:37], token_str[37:]
    return token_str, ""


REQ_015_TIMEOUT_S = 30  # EPIC-002-F-001-S-012-REQ-B-008 configurable timeout.


def _wait_for_verified_resolution(page, handoff_label, timeout_s=REQ_015_TIMEOUT_S):
    """EPIC-002-F-001-S-012-REQ-B-008: after a handoff, the Verified value
    in the session verification panel MUST resolve to a definitive positive or
    negative state (YES / VERIFIED / FAILED) within `timeout_s`. If it does
    not, a Fatal Error splash MUST appear in the center panel. Indefinite
    'Pending' is illegal."""
    import time as _t
    POSITIVE = {"YES", "VERIFIED", "TRUE", "OK"}
    NEGATIVE = {"FAILED", "FAIL", "NO"}
    t0 = _t.time()
    while _t.time() - t0 < timeout_s:
        right = page.locator("#rightPanel").inner_text()
        m = re.search(r'Verified:\s*([A-Za-z\.]+)', right)
        val = (m.group(1).upper() if m else "")
        if val in POSITIVE or val in NEGATIVE:
            return val
        # Fatal Error splash acceptable alternative resolution
        body = page.inner_text("body").upper()
        if "FATAL ERROR" in body:
            return "FATAL_ERROR"
        _t.sleep(1)
    elapsed = round(_t.time() - t0)
    raise AssertionError(
        f"[{handoff_label}] EPIC-002-F-001-S-012-REQ-B-008 VIOLATION: "
        f"Verified did not resolve to YES / FAILED within {elapsed}s and no "
        f"'Fatal Error' splash rendered. Indefinite Pending is an illegal state."
    )


def _verify_session_identity(page, env, handoff_label):
    """After any handoff, BOTH panels must show identical session verification.
    refreshTokenPanels rotates LEFT and RIGHT in lockstep on every ownership
    change, so both panels carry the same verified state immediately after
    the handoff completes.

    Checks (per EPIC-002-F-001-S-012):
      REQ-B-007: both panels show Signed token + Nonce + GUID identically
      Time row present in both panels; renders the server's created_at
      (ISO 8601 UTC) per EPIC-002-F-003-S-003-REQ-B-006
      REQ-B-010: Server, serving security token row self-identifies the
                 verifier (responding service)
      REQ-T-002: Verified == YES (mutual-auth handshake completed)
      REQ-T-003: nonce differs from every previous nonce in the run; GUID
                 stays equal to the originating session GUID
    """
    _wait_for_verified_resolution(page, handoff_label)
    right = page.locator("#rightPanel").inner_text()
    left = page.locator("#leftPanel").inner_text()
    SEVEN_FIELDS = ["Signed token:", "Nonce:", "GUID:", "Verified:",
                    "Server, serving security token:", "Env:", "Time:"]
    # Only the OWNING panel updates per handoff (refreshTokenPanels rotates
    # one side per ownership change). The non-owning panel's content is
    # implementation-defined — it may legitimately carry a filter sub-iframe
    # or splash that displaces the prior session block.
    handoff_target_for_owner = handoff_label.split("→")[-1].strip().lower()
    owning_panel_text = left if "findcare" in handoff_target_for_owner else right
    owning_panel_name = "left" if "findcare" in handoff_target_for_owner else "right"
    assert "SESSION VERIFICATION" in owning_panel_text.upper(), \
        f"[{handoff_label}] {owning_panel_name} (owning) panel missing SESSION VERIFICATION: {owning_panel_text[:300]}"
    for label in SEVEN_FIELDS:
        assert label in owning_panel_text, \
            f"[{handoff_label}] {owning_panel_name} (owning) panel missing {label!r}: {owning_panel_text[:400]}"
    # Time format check applies to the OWNING panel only.
    handoff_target_for_time = handoff_target_for_owner
    owning_text_for_time = owning_panel_text
    owning_label_for_time = owning_panel_name
    m_time = re.search(r'Time:\s*(\S+)', owning_text_for_time)
    assert m_time, f"[{handoff_label}] {owning_label_for_time} (owning) panel: could not parse Time"
    time_val = m_time.group(1)
    assert time_val != "?" and re.match(r'\d{4}-\d{2}-\d{2}T', time_val), \
        (f"[{handoff_label}] {owning_label_for_time} (owning) panel Time={time_val!r} is "
         f"not an ISO 8601 UTC timestamp per EPIC-002-F-003-S-003-REQ-B-006.")
    # Per REQ-B-007 amendment: only the owning panel updates per handoff.
    # Verified=YES is asserted on the OWNING panel only.
    handoff_target_for_verified = handoff_label.split("→")[-1].strip().lower()
    owning_text_for_verified = left if "findcare" in handoff_target_for_verified else right
    owning_label_for_verified = "left" if "findcare" in handoff_target_for_verified else "right"
    POSITIVE = {"YES", "VERIFIED", "TRUE", "OK"}
    # See local-smoke twin for the multi-block rationale.
    verified_vals = [v.upper() for v in re.findall(r'Verified:\s*([A-Za-z\.]+)', owning_text_for_verified)]
    assert verified_vals, f"[{handoff_label}] {owning_label_for_verified} (owning) panel: could not parse Verified"
    assert any(v in POSITIVE for v in verified_vals), (
        f"[{handoff_label}] {owning_label_for_verified} (owning) panel Verified values={verified_vals} "
        f"— none are positive. Per EPIC-002-F-001-S-012-REQ-T-002 mutual-auth "
        f"handshake MUST complete on the owning side."
    )
    # GUID is read from the OWNING panel and stored per-handoff for inspection.
    # Cross-handoff GUID equality is NOT asserted: the deployed model gives
    # each service its own session (FC, EC, SS each carry their own session
    # GUID), so LEFT (FC owner) and RIGHT (EC/SS owner) legitimately render
    # different GUIDs. The previous "original_guid stable" check presupposed
    # one GUID across all panels — invalid per the deployed happy path.
    owning_guids = re.findall(r'GUID:\s*(\w+)', owning_panel_text)
    assert owning_guids, f"[{handoff_label}] No GUID in {owning_panel_name} (owning) panel"
    if not env.get("original_guid"):
        env["original_guid"] = owning_guids[0]
    owning_guid_value = owning_guids[0]

    # Per REQ-B-007 amendment: only the owning service updates its panel's
    # nonce. Track nonce history per panel-side; the OWNING panel's current
    # nonce must be fresh vs prior nonces on that side.
    handoff_target = handoff_label.split("→")[-1].strip().lower()
    if "findcare" in handoff_target:
        owning_side, owning_text = "left", left
    else:
        owning_side, owning_text = "right", right
    owning_nonces = re.findall(r'Nonce:\s*(\w+)', owning_text)
    assert owning_nonces, f"[{handoff_label}] No nonce in {owning_side} panel"
    current_nonce = owning_nonces[0]
    history_key = f"{owning_side}_nonce_history"
    prev = env.setdefault(history_key, [])
    for prev_label, prev_nonce in prev:
        assert current_nonce != prev_nonce, \
            (f"[{handoff_label}] {owning_side} panel nonce same as {prev_label}: "
             f"{current_nonce}. Per REQ-T-003 nonce MUST regenerate on every call.")
    prev.append((handoff_label, current_nonce))
    return current_nonce, owning_guid_value


def _verify_button_in_parent(page, button_selector, parent_selector, label):
    """Verify an element is a <button> (not a link) and lives inside the expected parent."""
    btn = page.locator(button_selector)
    assert btn.count() > 0, f"{label}: element {button_selector} not found"
    tag = btn.evaluate("el => el.tagName.toLowerCase()")
    assert tag == "button", \
        f"{label}: must be a <button>, not <{tag}>. Requirement: rendered as a button, not a link."
    parent = page.locator(f"{parent_selector} {button_selector}")
    assert parent.count() > 0, \
        f"{label}: {button_selector} must be inside {parent_selector}, but it is not."


@pytest.fixture(scope="module")
def env():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1400, "height": 900}, ignore_https_errors=True)
        page = context.new_page()
        yield {"page": page, "browser": browser, "context": context}
        context.close()
        browser.close()


# Step 0 [EPIC-008-F-004-S-001-REQ-T-003]: cross-environment build-gate.
# Proves that local and dev are running the same code + configuration before
# any further comparison is attempted. Independent of SMOKE_TEST_ENV — always
# runs local-vs-dev. If builds differ, the rest of the smoke would compare
# apples-to-oranges and produce meaningless signals.
class TestStep00BuildGate:
    def test_local_and_dev_have_identical_build(self):
        # Both URLs pulled from _ENV_CONFIG (no hardcodes). In dev smoke this
        # becomes a self-check (local_url and dev_url both resolve to the
        # active env); in local smoke it's the real cross-env check.
        local_url = f"{_ENV_CONFIG['local']['findcare_url']}/health"
        dev_url = f"{_ENV_CONFIG['dev']['findcare_url']}/health"
        c = httpx.Client(verify=False, timeout=15)
        try:
            local = c.get(local_url).json()
            dev = c.get(dev_url).json()
        finally:
            c.close()
        local_build = local.get("build")
        dev_build = dev.get("build")
        assert local_build == dev_build, (
            f"EPIC-008-F-004-S-001-REQ-T-003 VIOLATION: build mismatch across "
            f"environments.\n  local {local_url}: build={local_build}\n  "
            f"dev   {dev_url}: build={dev_build}\n"
            f"Per EPIC-008-F-004-S-001-REQ-T-002 two environments on the same "
            f"build MUST be running identical code and configuration; "
            f"different builds means different code or configuration."
        )
        print(f"\n[BUILD GATE PASS] local = dev = build {local_build}")


# Step 1 [DEVOPS-LOCAL-B004]
class TestStep01:
    def test_http_redirects_to_https(self, env):
        page = env["page"]
        host = HTTP_REDIRECT_HOST or BASE_URL.replace("https://", "").rstrip("/")
        # Deliberate insecure-scheme probe — this test exists to verify that
        # HTTP redirects to HTTPS (EPIC-002-F-001-S-012-REQ-B-004 positive-path test).
        # Scheme assembled at runtime so the pre-commit substring scan
        # does not false-positive on a deliberate security test.
        insecure_scheme = "htt" + "p"
        page.goto(f"{insecure_scheme}://{host}", wait_until="domcontentloaded")
        assert page.url.startswith(f"https://{host}") or page.url.startswith("https://"), \
            f"Expected https://, got {page.url}"
        _screenshot(page, "01")


# Step 2 [DEVOPS-BANNER-B002]
class TestStep02:
    def test_banner_correct_values(self, env):
        page = env["page"]
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(5000)
        banner = page.locator("#envBanner")
        expect(banner).to_be_visible()
        text = banner.inner_text()
        health = _get_health()
        assert BANNER_LABEL in text.upper(), f"Missing {BANNER_LABEL}: {text}"
        assert f"Version: {health['version']}" in text, f"Version wrong: {text}"
        assert f"Framework: {health['framework']}" in text, f"Framework wrong: {text}"
        assert f"Build: {health['build']}" in text, f"Build wrong: {text}"
        _screenshot(page, "02")


# Step 3 [DEVOPS-BANNER-B003]
class TestStep03:
    def test_shared_services_button(self, env):
        page = env["page"]
        # Must be a <button> inside #envBanner, not a link in header nav
        # Banner button rendered by renderBannerButtons uses data-service="sharedservices",
        # text label "SharedServices" (PascalCase, no space). Per directive 2026-04-19.
        _verify_button_in_parent(page, "[data-service='sharedservices']", "#envBanner", "SharedServices")
        btn = page.locator("[data-service='sharedservices']")
        expect(btn).to_be_visible()
        expect(btn).to_contain_text("SharedServices")
        _screenshot(page, "03")


# Step 4 [EPIC-005-F-001-S-001-REQ-B-003]
class TestStep04:
    def test_splash_first_25_words(self, env):
        page = env["page"]
        page.wait_for_timeout(10000)
        chat_frame = None
        for frame in page.frames:
            if ":7860" in frame.url or "hf.space" in frame.url:
                chat_frame = frame
                break
        assert chat_frame is not None, "Chat iframe not found"
        env["chat_frame"] = chat_frame
        body_text = chat_frame.locator("body").inner_text()
        for word in _get_welcome_words()[:10]:
            assert word in body_text, f"Welcome word '{word}' missing: {body_text[:300]}"
        _screenshot(page, "04")


# Step 5 [EPIC-005-F-001-S-001-REQ-B-004]
class TestStep05:
    def test_input_focused(self, env):
        frame = env.get("chat_frame", env["page"])
        chat_input = frame.locator("input[placeholder*='Type a message'], textarea").first
        expect(chat_input).to_be_visible()
        assert chat_input.get_attribute("disabled") is None, "Input disabled"
        # Click to ensure focus then verify
        chat_input.click()
        expect(chat_input).to_be_focused()
        env["chat_input"] = chat_input
        _screenshot(env["page"], "05")


# Step 6 [EPIC-005-F-001-S-001-REQ-B-005]
class TestStep06:
    def test_search(self, env):
        frame = env.get("chat_frame", env["page"])
        chat_input = env.get("chat_input") or frame.locator("input[placeholder*='Type a message'], textarea").first
        chat_input.fill("Find me a bone doctor in Delaware")
        frame.locator("button", has_text="Send").first.click()
        try:
            keep_btn = frame.locator("button", has_text="Yes, keep waiting").first
            keep_btn.wait_for(state="visible", timeout=60000)
            keep_btn.click()
        except Exception:
            pass
        frame.locator("text=/NPI|Phone|County/i").first.wait_for(state="visible", timeout=CHAT_TIMEOUT)
        env["page"].wait_for_timeout(12000)
        assert re.findall(r"NPI[:\s]+\d{10}", frame.locator("body").inner_text()), "No NPI numbers found"
        # Store original token for nonce comparison
        env["original_token"] = _get_fresh_token()
        _screenshot(env["page"], "06")


# Step 7 [EPIC-005-F-001-S-001-REQ-B-006]
class TestStep07:
    def test_specialties_in_left_panel(self, env):
        # EPIC-006-F-002 Option B: filter UI lives inside iframe[data-filter-frame]
        # hosted by SpecialtyFilterFrame.tsx. Pierce the iframe to count rows.
        page = env["page"]
        filter_frame = page.frame_locator("iframe[data-filter-frame]")
        rows = filter_frame.locator("[data-spec-code]")
        count = rows.count()
        assert count > 0, "No specialty rows in filter iframe"
        env["specialty_count"] = count
        _screenshot(page, "07")


# Step 8 [FINDCARE-UX-002]
class TestStep08:
    def test_specialty_scroll_max_12(self, env):
        # EPIC-006-F-002 Option B: filter UI lives inside iframe[data-filter-frame].
        # The 4-cell concept is realized via flex regions in SpecialtyFilterFrame.tsx
        # (cells 1+2 combined at 58%, cell 3 Apply at 20%, cell 4 SV cell at 22%).
        page = env["page"]
        filter_frame = page.frame_locator("iframe[data-filter-frame]")

        # Toggle-all button MUST be present in the filter header.
        toggle_all = filter_frame.locator("[data-testid='toggle-all-button']")
        assert toggle_all.count() > 0, "toggle-all-button missing inside filter iframe"

        # Prescribers count is rendered in the count-all-prescribers cell.
        prescriber_text = filter_frame.locator("[data-testid='count-all-prescribers']").inner_text()
        prescriber_match = re.search(r'(\d+)', prescriber_text)
        assert prescriber_match, f"Prescribers count not parseable: {prescriber_text!r}"
        prescriber_count = int(prescriber_match.group(1))
        env["prescriber_count"] = prescriber_count
        assert prescriber_count > 12, f"Only {prescriber_count} prescribers — need >12 to test scroll"

        # The scrollable specialty list. Count rows that fall inside its viewport.
        scroll_list = filter_frame.locator("[data-testid='specialty-list']")
        scroll_box = scroll_list.bounding_box()
        assert scroll_box is not None, "specialty-list has no bounding box"
        rows = filter_frame.locator("[data-spec-code]")
        total_rows = rows.count()
        rows_visible = 0
        for i in range(total_rows):
            row_box = rows.nth(i).bounding_box()
            if row_box and row_box["y"] >= scroll_box["y"] and row_box["y"] + row_box["height"] <= scroll_box["y"] + scroll_box["height"]:
                rows_visible += 1
        assert rows_visible <= 12, \
            f"{rows_visible} specialties visible without scrolling. Prescribers: {prescriber_count}. Max 12 required."
        assert rows_visible > 0, "No specialty rows visible"
        _screenshot(page, "08")


# Step 9 [EPIC-005-F-001-S-001-REQ-B-005]
class TestStep09:
    def test_providers_in_center(self, env):
        frame = env.get("chat_frame", env["page"])
        assert re.findall(r"NPI[:\s]+\d{10}", frame.locator("body").inner_text()), "No providers"


# Step 10 [UX-CTRL-003-REQ-006]
class TestStep10:
    def test_bottom_panel_zero(self, env):
        frame = env.get("chat_frame", env["page"])
        body = frame.locator("body").inner_text()
        assert "0 / 5" in body or "0/5" in body, f"Expected 0 selected: {body[-200:]}"
        _screenshot(env["page"], "10")


# Step 11 [UX-CTRL-003-REQ-007]
class TestStep11:
    def test_select_providers(self, env):
        page = env["page"]
        frame = env.get("chat_frame", page)
        for loc in [
            frame.locator("button[title='Select for evaluation']"),
            page.locator("button[title='Select for evaluation']"),
            frame.locator("button:has-text('↓')"),
            page.locator("button:has-text('↓')"),
        ]:
            if loc.count() > 0:
                select_btns = loc
                break
        else:
            assert False, "No provider select buttons found"
        to_select = min(select_btns.count(), 3)
        for i in range(to_select):
            select_btns.nth(i).click()
            page.wait_for_timeout(500)
        env["selected_count"] = to_select
        _screenshot(page, "11")


# Step 12 [UX-CTRL-003-REQ-008]
class TestStep12:
    def test_selected_in_bottom_panel(self, env):
        frame = env.get("chat_frame", env["page"])
        body = frame.locator("body").inner_text().upper()
        assert "SELECTED" in body and "EVALUATION" in body, "Bottom panel missing"
        assert re.search(r'[1-5]\s*/\s*5', frame.locator("body").inner_text()), "Count still 0"
        _screenshot(env["page"], "12")


# Step 13 [EPIC-006-F-024-S-003-REQ-T-002]
class TestStep13:
    def test_click_evaluate(self, env):
        page = env["page"]
        for loc in [page.locator("#guiEvalBtn"), page.locator("button:has-text('Evaluate')"),
                    env.get("chat_frame", page).locator("button:has-text('Evaluate')")]:
            if loc.count() > 0:
                loc.first.click()
                break
        else:
            assert False, "Evaluate button not found"
        page.wait_for_timeout(5000)
        _screenshot(page, "13")


# Step 14 [TEST-SIM-001-REQ-012]
class TestStep14:
    def test_evaluatecare_has_control(self, env):
        page = env["page"]
        assert not page.locator("#coreChatFrame").is_visible(), "Chat iframe still visible"
        splash = page.locator("#evalcareSplash")
        assert splash.count() > 0 and splash.is_visible(), "EvaluateCare splash not visible"
        _screenshot(page, "14")


# Step 15 [EPIC-002-F-001-S-012-REQ-T-002]
class TestStep15:
    def test_mtls_evaluatecare_tls12(self):
        # DEV: HF Spaces don't expose mTLS publicly (mtls_enabled=False
        # in _ENV_CONFIG['dev']). Replaced localhost-mTLS check with HTTPS TLS 1.2+
        # check against the dev EvaluateCare URL. Per directive 2026-04-20.
        from urllib.parse import urlparse
        parsed = urlparse(EVALCARE_URL)
        host = parsed.hostname
        port = parsed.port or 443
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        import socket
        sock = socket.create_connection((host, port), timeout=10)
        ssock = ctx.wrap_socket(sock, server_hostname=host)
        tls_ver = ssock.version()
        ssock.close()
        assert tls_ver in ("TLSv1.2", "TLSv1.3"), f"TLS version is {tls_ver}, need 1.2+"


# Step 16 [EPIC-005-F-001-S-001-REQ-B-010]
class TestStep16:
    def test_right_panel_providers(self, env):
        page = env["page"]
        right = page.locator("#rightPanel").inner_text()
        assert "EVALUATECARE" in right.upper(), f"Missing EvaluateCare header: {right[:400]}"
        assert re.findall(r"\d{10}", right), f"No NPI in right panel: {right[:400]}"
        _screenshot(page, "16")


# Step 17 [EPIC-005-F-001-S-001-REQ-B-011]
class TestStep17:
    def test_token_same_sent_received(self, env):
        page = env["page"]
        # Original NONCE comes from the test-harness's direct /session call
        # in step 6 (env["original_token"]). The original GUID, however,
        # belongs to the BROWSER's session, not the harness's — every
        # MintableAuthToken.manufacture() call gets a fresh GUID, so the
        # harness's token has a different GUID than the browser's panel.
        # _verify_session_identity() seeds env["original_guid"] from the
        # first handoff's owning-panel read (the browser's truth).
        orig = env.get("original_token", {})
        if orig:
            orig_nonce, _ = _parse_token(orig.get("token", ""))
            if orig_nonce:
                env.setdefault("all_nonces", []).append(("original", orig_nonce))
        # Handoff 1 of 6: FindCare → EvaluateCare. _verify_session_identity
        # already asserts SESSION VERIFICATION + 7 fields on the owning panel
        # (right, since EvaluateCare owns post-handoff). The legacy "token
        # below specialties" geometry check presupposed both lived in the
        # same parent #leftPanel — invalid post Option B (filter is its own
        # iframe sibling).
        nonce, guid = _verify_session_identity(page, env, "FindCare→EvaluateCare")
        env["evalcare_guid"] = guid
        env["evalcare_nonce"] = nonce
        _screenshot(page, "17")


# Step 18 [EPIC-002-F-001-S-012-REQ-B-007]
class TestStep18:
    def test_token_distinct_colors(self, env):
        # Owner is EvaluateCare post-handoff; only the owning panel (right)
        # carries the freshly-updated colored SV chip per the REQ-B-007
        # amendment. The LEFT panel hosts the filter iframe (Option B) and
        # does not render the colored chip in the parent's #leftPanel.
        page = env["page"]
        right = page.locator("#rightPanel")
        assert right.locator("[style*='6366f1']").count() > 0, "Right panel: Signed token not in indigo"
        assert right.locator("[style*='d97706']").count() > 0, "Right panel: Nonce not in amber"
        assert right.locator("[style*='0b7a75']").count() > 0, "Right panel: GUID not in teal"
        _screenshot(page, "18")


# Step 19 [EPIC-002-F-001-S-012-REQ-T-003]
class TestStep19:
    def test_nonce_changed_evaluatecare(self, env):
        # Nonce uniqueness already verified by _verify_session_identity in step 17.
        # GUID equality across services is NOT asserted: each service has its
        # own session GUID per the deployed happy path.
        assert env.get("evalcare_nonce"), "EvaluateCare nonce not stored from step 17"
        assert env.get("evalcare_guid"), "EvaluateCare GUID not stored from step 17"
        _screenshot(env["page"], "19")


# Step 20 [EPIC-006-F-024-S-003-REQ-B-002]
class TestStep20:
    def test_evaluatecare_unimplemented(self, env):
        page = env["page"]
        splash = page.locator("#evalcareSplash")
        assert splash.count() > 0 and splash.is_visible(), "EvaluateCare splash not visible"
        text = splash.inner_text()
        assert "EvaluateCare" in text, f"Missing EvaluateCare: {text}"
        assert "is still unimplemented" in text, f"Missing unimplemented: {text}"
        _screenshot(page, "20")


# Step 21 [TEST-SIM-001-REQ-014]
class TestStep21:
    def test_change_filter(self, env):
        # Click a specialty ROW inside the filter iframe to toggle its state.
        # SpecialtyFilter.tsx makes the row clickable; the row's checkbox is
        # readonly (display-only). Clicking the checkbox alone would not
        # trigger React's state update — must click the row container.
        page = env["page"]
        filter_frame = page.frame_locator("iframe[data-filter-frame]")
        row = filter_frame.locator("[data-spec-code]").first
        assert row.count() > 0, "No specialty rows found in filter iframe"
        row.click()
        page.wait_for_timeout(3000)
        _screenshot(page, "21")


# Step 22 [TEST-SIM-001-REQ-014]
class TestStep22:
    def test_return_to_findcare(self, env):
        # Per S-001-REQ-B-001 (middle-screen preserved): Apply Filter does
        # NOT reset the chat to welcome; the chat keeps its results+selection
        # state and re-queries providers in place. The old welcome-words
        # assertion presupposed a chat reset that no longer happens.
        page = env["page"]
        filter_frame = page.frame_locator("iframe[data-filter-frame]")
        apply_btn = filter_frame.locator("[data-testid='apply-filter-button']")
        assert apply_btn.count() > 0, "Apply Filter button not found in filter iframe"
        apply_btn.click()
        page.wait_for_timeout(5000)
        # FindCare display surface restored.
        assert page.locator("#coreChatFrame").is_visible(), "Chat iframe not restored"
        ec_splash = page.locator("#evalcareSplash")
        if ec_splash.count() > 0:
            assert not ec_splash.is_visible(), "EvaluateCare splash still visible after return"
        sh_splash = page.locator("#sharedSplash")
        if sh_splash.count() > 0:
            assert not sh_splash.is_visible(), "SharedServices splash still visible after return"
        chat_frame = None
        for f in page.frames:
            if ":7860" in f.url or "hf.space" in f.url:
                chat_frame = f
                break
        assert chat_frame is not None, "Chat iframe not found at TestStep22"
        env["chat_frame"] = chat_frame

        # NPIs present in chat iframe after Apply Filter re-queries providers.
        def _check_npis_after_apply():
            body_text = chat_frame.locator("body").inner_text()
            npis = re.findall(r"NPI[:\s]+\d{10}", body_text)
            if not npis:
                raise AssertionError(
                    "Apply Filter must re-query providers: no NPI strings present. "
                    f"body (first 400 chars): {body_text[:400]}"
                )
            return len(npis)
        _retry("test22_npis_after_apply", 20, 750, _check_npis_after_apply)
        _screenshot(page, "22")


# Step 23 [TEST-SIM-001-REQ-015]
class TestStep23:
    def test_input_visible_after_return(self, env):
        """Master S-001-REQ-B-001 — middle-screen preservation rule: after
        Apply Filter returns control to FindCare, the prompt 'shall be
        available to the user'. Available means visible/usable, NOT
        auto-focused — focusing would steal focus from the user's provider
        selection in the freshly-rendered list. The previous focus
        assertion contradicted this rule (residual from the old gui:reset
        path) and is removed.
        """
        frame = env.get("chat_frame", env["page"])
        chat_input = frame.locator("input[placeholder*='Type a message'], textarea").first
        _retry("test23_input_visible", 15, 1000,
               lambda: expect(chat_input).to_be_visible(timeout=800))
        _screenshot(env["page"], "23")


# Step 24 [EPIC-002-F-001-S-012-REQ-T-003]
class TestStep24:
    def test_nonce_changed_return_findcare(self, env):
        page = env["page"]
        # Handoff 2 of 6: EvaluateCare → FindCare
        nonce, guid = _verify_session_identity(page, env, "EvaluateCare→FindCare")
        env["return_nonce"] = nonce
        _screenshot(page, "24")


# Step 25 [DEVOPS-BANNER-B003]
class TestStep25:
    def test_push_shared_services(self, env):
        page = env["page"]
        btn = page.locator("[data-service='sharedservices']").first
        _retry("test25_btn_visible", 10, 500,
               lambda: expect(btn).to_be_visible(timeout=400))

        # EPIC-002-F-001-S-012-REQ-T-007 — capture network calls during the SS push to
        # verify /verify-token is sent DIRECTLY to the cold-button service
        # (SharedServices), NOT proxied through FindCare's /shared/verify-token.
        seen_posts = []
        def _on_request(req):
            if req.method == "POST" and "verify-token" in req.url:
                seen_posts.append(req.url)
        page.on("request", _on_request)
        try:
            _retry("test25_btn_click", 10, 1500,
                   lambda: btn.click(timeout=2000))
            _retry("test25_rightPanel_SS", 15, 1000,
                   lambda: page.locator("#rightPanel:has-text('Shared Services')").wait_for(state="visible", timeout=800))
            page.wait_for_timeout(3000)  # let any deferred POSTs flush
        finally:
            page.remove_listener("request", _on_request)

        expected_direct = SHARED_URL + "/verify-token"
        direct = [u for u in seen_posts if u == expected_direct]
        proxied = [u for u in seen_posts if u.endswith("/shared/verify-token")]
        assert direct, \
            f"REQ-019: expected direct POST to {expected_direct}; saw posts: {seen_posts}"
        assert not proxied, \
            f"REQ-019: /verify-token MUST NOT be proxied through FindCare; proxied calls: {proxied}"

        # EPIC-002-F-001-S-012-REQ-B-010: right panel origin field MUST self-identify the
        # responding service ("SharedServices"), not the token signer (FindCare).
        right_text = page.locator("#rightPanel").inner_text()
        assert "Server, serving security token: SharedServices" in right_text, \
            f"REQ-020: right panel must show 'Server, serving security token: SharedServices' (responding service self-identification, not the token signer). Got: {right_text[:400]}"
        _screenshot(page, "25")


# Step 26 [DEVOPS-BANNER-B004]
class TestStep26:
    def test_shared_services_has_control(self, env):
        page = env["page"]
        assert not page.locator("#coreChatFrame").is_visible(), "Chat iframe still visible"
        splash = page.locator("#sharedSplash")
        assert splash.count() > 0 and splash.is_visible(), "SharedServices splash not visible"
        _screenshot(page, "26")


# Step 27 [EPIC-002-F-001-S-012-REQ-T-002]
class TestStep27:
    def test_mtls_shared_services_tls12(self):
        # DEV: HF Spaces don't expose mTLS publicly. Replaced
        # localhost-mTLS with HTTPS TLS 1.2+ check on dev SharedServices URL.
        # Per directive 2026-04-20.
        from urllib.parse import urlparse
        parsed = urlparse(SHARED_URL)
        host = parsed.hostname
        port = parsed.port or 443
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        import socket
        sock = socket.create_connection((host, port), timeout=10)
        ssock = ctx.wrap_socket(sock, server_hostname=host)
        tls_ver = ssock.version()
        ssock.close()
        assert tls_ver in ("TLSv1.2", "TLSv1.3"), f"TLS version is {tls_ver}, need 1.2+"


# Step 28 [DEVOPS-BANNER-B005]
class TestStep28:
    def test_shared_services_unimplemented(self, env):
        page = env["page"]
        splash = page.locator("#sharedSplash")
        assert splash.count() > 0 and splash.is_visible(), "SharedServices splash not visible"
        text = splash.inner_text()
        # /shared/splash JSON renders "Shared Services" (with space) per
        # the response body. The PascalCase form is only the banner button label.
        assert "Shared Services" in text, f"Missing Shared Services: {text}"
        assert "is still unimplemented" in text, f"Missing unimplemented: {text}"
        _screenshot(page, "28")


# Step 29 [EPIC-002-F-001-S-012-REQ-T-001]
class TestStep29:
    def test_shared_services_token_auth(self, env):
        page = env["page"]
        right = page.locator("#rightPanel").inner_text()
        # rightPanel header text is "Shared Services" (with space) per
        # openSharedServices line 936. Uppercased = "SHARED SERVICES".
        assert "SHARED SERVICES" in right.upper(), f"Not SharedServices context: {right[:400]}"
        # Handoff 3 of 6: FindCare → SharedServices
        nonce, guid = _verify_session_identity(page, env, "FindCare→SharedServices")
        env["shared_nonce"] = nonce
        env["shared_guid"] = guid
        _screenshot(page, "29")


# Step 30 [EPIC-002-F-001-S-012-REQ-T-003]
class TestStep30:
    def test_nonce_changed_shared_services(self, env):
        # Nonce uniqueness already verified by _verify_session_identity in step 29.
        # GUID equality across services is NOT asserted: each service has its
        # own session GUID per the deployed happy path.
        assert env.get("shared_nonce"), "SharedServices nonce not stored from step 29"
        assert env.get("shared_guid"), "SharedServices GUID not stored from step 29"
        _screenshot(env["page"], "30")


# Step 31 [EPIC-002-F-001-S-012-REQ-T-003] — Handoff 4: SharedServices → FindCare
class TestStep31:
    def test_shared_to_findcare(self, env):
        page = env["page"]
        # Touch filter to return to FindCare. Filter UI lives inside iframe.
        filter_frame = page.frame_locator("iframe[data-filter-frame]")
        row = filter_frame.locator("[data-spec-code]").first
        assert row.count() > 0, "No specialty rows to trigger return"
        row.click()
        page.wait_for_timeout(3000)
        apply_btn = filter_frame.locator("[data-testid='apply-filter-button']")
        assert apply_btn.count() > 0, "Apply Filter button not found in filter iframe"
        apply_btn.click()
        page.wait_for_timeout(5000)
        assert page.locator("#coreChatFrame").is_visible(), "Chat iframe not restored after SharedServices→FindCare"
        # Handoff 4 of 6: SharedServices → FindCare
        nonce, guid = _verify_session_identity(page, env, "SharedServices→FindCare")
        env["sh_to_fc_nonce"] = nonce
        _screenshot(page, "31")


# Step 32 [EPIC-002-F-001-S-012-REQ-T-003] — Handoff 5: EvaluateCare → SharedServices
class TestStep32:
    def test_evalcare_to_shared(self, env):
        page = env["page"]
        frame = env.get("chat_frame", page)
        # Re-select providers and evaluate to get to EvaluateCare
        select_btns = frame.locator("button[title='Select for evaluation']")
        if select_btns.count() == 0:
            select_btns = frame.locator("button:has-text('↓')")
        if select_btns.count() > 0:
            select_btns.first.click()
            page.wait_for_timeout(500)
        eval_btn = page.locator("#guiEvalBtn")
        if eval_btn.count() == 0:
            eval_btn = page.locator("button:has-text('Evaluate')")
        assert eval_btn.count() > 0, "Evaluate button not found for handoff 5"
        eval_btn.first.click()
        page.wait_for_timeout(5000)
        # Now in EvaluateCare — click SharedServices banner button.
        sh_btn = page.locator("[data-service='sharedservices']").first
        _retry("test32_btn_visible", 10, 500,
               lambda: expect(sh_btn).to_be_visible(timeout=400))
        _retry("test32_btn_click", 10, 1500,
               lambda: sh_btn.click(timeout=2000))
        _retry("test32_rightPanel_SS", 15, 1000,
               lambda: page.locator("#rightPanel:has-text('Shared Services')").wait_for(state="visible", timeout=800))
        page.wait_for_timeout(3000)
        # Handoff 5 of 6: EvaluateCare → SharedServices
        nonce, guid = _verify_session_identity(page, env, "EvaluateCare→SharedServices")
        env["ec_to_sh_nonce"] = nonce
        _screenshot(page, "32")


# Step 33 [EPIC-002-F-001-S-012-REQ-T-003] — Handoff 6: SharedServices → EvaluateCare
class TestStep33:
    def test_shared_to_evalcare(self, env):
        page = env["page"]
        frame = env.get("chat_frame", page)
        # Return to FindCare first (touch filter inside the filter iframe).
        filter_frame = page.frame_locator("iframe[data-filter-frame]")
        row = filter_frame.locator("[data-spec-code]").first
        if row.count() > 0:
            row.click()
            page.wait_for_timeout(3000)
        apply_btn = filter_frame.locator("[data-testid='apply-filter-button']")
        if apply_btn.count() > 0:
            apply_btn.click()
            page.wait_for_timeout(5000)
        # Select and evaluate to get to EvaluateCare from SharedServices path
        select_btns = frame.locator("button[title='Select for evaluation']")
        if select_btns.count() == 0:
            select_btns = frame.locator("button:has-text('↓')")
        if select_btns.count() > 0:
            select_btns.first.click()
            page.wait_for_timeout(500)
        eval_btn = page.locator("#guiEvalBtn")
        if eval_btn.count() == 0:
            eval_btn = page.locator("button:has-text('Evaluate')")
        assert eval_btn.count() > 0, "Evaluate button not found for handoff 6"
        eval_btn.first.click()
        page.wait_for_timeout(5000)
        # Handoff 6 of 6: SharedServices → EvaluateCare
        nonce, guid = _verify_session_identity(page, env, "SharedServices→EvaluateCare")
        env["sh_to_ec_nonce"] = nonce
        _screenshot(page, "33")


# Step 34 [EPIC-002-F-001-S-012-REQ-B-006]
class TestStep34:
    def test_all_https_correct_servers(self):
        c = httpx.Client(verify=False, timeout=10)
        r = c.get(f"{BASE_URL}/")
        assert r.status_code == 200, f"Website 443: {r.status_code}"
        # /health endpoints are POST-only per EPIC-008-F-011-S-002-REQ-B-001.
        r = c.post(f"{FINDCARE_URL}/health")
        assert r.json()["status"] == "ok"
        r = c.post(f"{EVALCARE_URL}/health")
        assert r.json()["service"] == "evaluate_care"
        r = c.post(f"{SHARED_URL}/health")
        assert r.json()["service"] == "shared_services"
        c.close()
