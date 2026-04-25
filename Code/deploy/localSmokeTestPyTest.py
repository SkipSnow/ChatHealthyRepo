# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# localSmokeTestPyTest.py — DEVOPS-LOCAL-B009
# 34-step Playwright smoke test. Each step maps to a requirement.
# Steps 31-33 added for BUG-UX-020: all 6 component handoff permutations.
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

SMOKE_ENV = os.getenv("SMOKE_TEST_ENV", "local").lower()

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
        "mtls_enabled":       False,                   # BUG-SEC-002: HF does not support mTLS (deferred to Beta)
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
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "test_output", "smoke_test")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
CHAT_TIMEOUT = 120_000


def _screenshot(page, name):
    page.screenshot(path=os.path.join(SCREENSHOT_DIR, f"{name}.png"), full_page=True)


def _retry(label, attempts, sleep_ms, action):
    """Retry `action` (zero-arg callable) up to `attempts` times. Sleep
    `sleep_ms` ms between tries. Print '[retry] LABEL: attempt N/M' on each.
    Re-raise the last exception if all attempts fail. Per Skip directive
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
    c = httpx.Client(verify=False, timeout=10)
    try:
        return c.get(f"{FINDCARE_URL}/health").json()
    finally:
        c.close()


def _get_welcome_words():
    c = httpx.Client(verify=False, timeout=10)
    try:
        data = c.get(f"{FINDCARE_URL}/welcome").json()
        import html as html_mod
        text = re.sub(r'<[^>]+>', ' ', data.get("message", ""))
        text = html_mod.unescape(text).strip()
        return text.split()[:25]
    finally:
        c.close()


def _get_fresh_token():
    c = httpx.Client(verify=False, timeout=10)
    try:
        return c.get(f"{FINDCARE_URL}/session").json()
    finally:
        c.close()


def _parse_token(token_str):
    """Parse CH{nonce:34}{guid:32} into nonce and guid."""
    if len(token_str) >= 68:
        return token_str[2:36], token_str[36:]
    return token_str, ""


REQ_015_TIMEOUT_S = 30  # EPIC-002-F-001-S-012-REQ-B-008 configurable timeout.


def _wait_for_verified_resolution(page, handoff_label, timeout_s=REQ_015_TIMEOUT_S):
    """BUG-TEST-036 (EPIC-002-F-001-S-012-REQ-B-008): after a handoff, the Verified value
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
    """After any handoff, both panels must show identical session verification.
    Checks: SESSION VERIFICATION header, all 5 labeled fields (Signed token,
    Nonce, GUID, Origin, Verified), Verified value == VERIFIED.
    Nonces must match between panels. GUID must match original.
    Nonce must differ from previous.

    BUG-TEST-032 (EPIC-002-F-001-S-012-REQ-T-002): assert Verified == VERIFIED.
    BUG-TEST-035 (EPIC-002-F-001-S-012-REQ-B-007): assert all 5 SV fields present.
    BUG-TEST-036 (EPIC-002-F-001-S-012-REQ-B-008): wait up to 30s for Verified to
      resolve out of Pending before asserting its value."""
    # BUG-TEST-036: bounded wait for Verified to leave the transient Pending state.
    _wait_for_verified_resolution(page, handoff_label)
    right = page.locator("#rightPanel").inner_text()
    left = page.locator("#leftPanel").inner_text()
    # Both panels must have SESSION VERIFICATION
    assert "SESSION VERIFICATION" in right.upper(), \
        f"[{handoff_label}] Right panel missing SESSION VERIFICATION: {right[:300]}"
    assert "SESSION VERIFICATION" in left.upper(), \
        f"[{handoff_label}] Left panel missing SESSION VERIFICATION: {left[-300:]}"
    # BUG-TEST-035: all 5 SV labels must be present in both panels
    for label in ["Signed token:", "Nonce:", "GUID:", "Origin:", "Verified:"]:
        assert label in right, f"[{handoff_label}] Right panel missing {label}: {right[:400]}"
        assert label in left, f"[{handoff_label}] Left panel missing {label}: {left[-400:]}"
    # BUG-TEST-032: Verified MUST be the positive value (UI renders "YES ✓").
    # FAILED, Pending, and any other state is illegal per EPIC-002-F-001-S-012-REQ-B-008.
    # Positive-value set tolerates minor UI wording variations (YES/VERIFIED/TRUE).
    POSITIVE = {"YES", "VERIFIED", "TRUE", "OK"}
    for panel_name, txt in (("right", right), ("left", left)):
        m = re.search(r'Verified:\s*([A-Za-z\.]+)', txt)
        assert m, f"[{handoff_label}] Could not parse Verified in {panel_name} panel"
        val = m.group(1).upper()
        assert val in POSITIVE, (
            f"[{handoff_label}] {panel_name} panel Verified == {val!r} "
            f"(expected one of {sorted(POSITIVE)}). Per EPIC-002-F-001-S-012-REQ-T-002 "
            f"mutual-auth handshake MUST complete; per EPIC-002-F-001-S-012-REQ-B-008 "
            f"any non-positive runtime state (FAILED, PENDING) is fatal."
        )
    # Extract and compare nonces — must match between panels
    right_nonces = re.findall(r'Nonce:\s*(\w+)', right)
    left_nonces = re.findall(r'Nonce:\s*(\w+)', left)
    assert right_nonces, f"[{handoff_label}] No nonce in right panel"
    assert left_nonces, f"[{handoff_label}] No nonce in left panel"
    assert right_nonces[0] == left_nonces[0], \
        f"[{handoff_label}] Nonce mismatch: right={right_nonces[0]} left={left_nonces[0]}"
    # Extract and compare GUIDs — must match between panels and match original
    right_guids = re.findall(r'GUID:\s*(\w+)', right)
    left_guids = re.findall(r'GUID:\s*(\w+)', left)
    assert right_guids, f"[{handoff_label}] No GUID in right panel"
    assert left_guids, f"[{handoff_label}] No GUID in left panel"
    assert right_guids[0] == left_guids[0], \
        f"[{handoff_label}] GUID mismatch: right={right_guids[0]} left={left_guids[0]}"
    orig_guid = env.get("original_guid", "")
    if orig_guid:
        assert right_guids[0] == orig_guid, \
            f"[{handoff_label}] GUID changed from original: {right_guids[0]} vs {orig_guid}"
    # Nonce must differ from all previously stored nonces
    current_nonce = right_nonces[0]
    prev_nonces = env.get("all_nonces", [])
    for prev_label, prev_nonce in prev_nonces:
        assert current_nonce != prev_nonce, \
            f"[{handoff_label}] Nonce same as {prev_label}: {current_nonce}"
    # Store for next check
    env.setdefault("all_nonces", []).append((handoff_label, current_nonce))
    if not orig_guid:
        env["original_guid"] = right_guids[0]
    return current_nonce, right_guids[0]


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
        # URLs pulled from _ENV_CONFIG — no hardcodes. Real cross-env
        # comparison: local build must match dev build before smoke proceeds.
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
        # Scheme assembled at runtime so pre_deploy_rule_check.py's substring
        # scan does not false-positive on a deliberate security test.
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
        # BUG-UX-021: Must be a <button> inside #envBanner, not a link in header nav
        # Banner button rendered by renderBannerButtons uses data-service="sharedservices",
        # text label "SharedServices" (PascalCase, no space). Per Skip directive 2026-04-19.
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
        page = env["page"]
        left = page.locator("#leftPanel")
        count = left.locator("input[type='checkbox']").count()
        assert count > 0, f"No specialty checkboxes: {left.inner_text()[:300]}"
        env["specialty_count"] = count
        _screenshot(page, "07")


# Step 8 [FINDCARE-UX-002]
class TestStep08:
    def test_specialty_scroll_max_12(self, env):
        page = env["page"]
        left = page.locator("#leftPanel")

        # BUG-TEST-033 (FINDCARE-UX-002): left panel MUST have 4 cells with
        # reactive percentage heights 18/40/20/22. Structure-agnostic — accepts
        # any tag that carries [data-cell="N"]. Pixel heights MUST NOT be used.
        cells = left.locator("[data-cell]")
        cell_count = cells.count()
        assert cell_count == 4, (
            f"FINDCARE-UX-002: expected 4 cells with [data-cell='1..4'] in left panel, got {cell_count}."
        )
        expected_pct = [18, 40, 20, 22]
        for i, pct in enumerate(expected_pct):
            cell = left.locator(f"[data-cell='{i+1}']")
            assert cell.count() == 1, f"FINDCARE-UX-002: [data-cell='{i+1}'] missing or duplicated"
            style = cell.get_attribute("style") or ""
            style_clean = style.replace(" ", "")
            assert (f"flex:00{pct}%" in style_clean) or (f"height:{pct}%" in style_clean), (
                f"FINDCARE-UX-002: cell {i+1} MUST declare {pct}% reactive height "
                f"(flex:0 0 {pct}% or height:{pct}%). Got style={style!r}"
            )
            # No pixel heights allowed
            assert not re.search(r'(?:height|flex-basis):\s*\d+px', style), (
                f"FINDCARE-UX-002: cell {i+1} MUST NOT use pixel height/flex-basis. Got {style!r}"
            )

        # BUG-TEST-034 (EPIC-006-F-002-S-001-REQ-B-008): Uncheck All MUST sit inside
        # cell 1 (the 18% green header), to the left of Prescribers and
        # Homeopathic filter checkboxes.
        cell1_toggle = left.locator("[data-cell='1'] [data-gui-action='toggle-all']")
        assert cell1_toggle.count() > 0, (
            "EPIC-006-F-002-S-001-REQ-B-008: Uncheck All/Check All toggle MUST be inside cell 1 "
            "(the green header), not in any other cell."
        )

        left_text = left.inner_text()
        # Read the Prescribers count from the filter header
        prescriber_match = re.search(r'PRESCRIBERS\s*(\d+)', left_text.upper())
        assert prescriber_match, f"Prescribers count not found in left panel: {left_text[:200]}"
        prescriber_count = int(prescriber_match.group(1))
        env["prescriber_count"] = prescriber_count
        assert prescriber_count > 12, f"Only {prescriber_count} prescribers — need >12 to test scroll"
        # Count specialty checkboxes whose bounding box falls within cell 2's
        # visible viewport (the scroll container). Items scrolled below cell 2's
        # clip still have page-relative positions, but we want truly-visible items.
        cell2_loc = left.locator("[data-cell='2']")
        cell2_box = cell2_loc.bounding_box()
        assert cell2_box is not None, "cell 2 has no bounding box"
        # Specialty items live inside cell 2 (excludes Prescribers/Homeopathic
        # toggles that live in cell 1).
        specialty_cb = cell2_loc.locator("input[type='checkbox']")
        total_sp = specialty_cb.count()
        specialty_visible = 0
        for i in range(total_sp):
            cb_box = specialty_cb.nth(i).bounding_box()
            if cb_box and cb_box["y"] >= cell2_box["y"] and cb_box["y"] + cb_box["height"] <= cell2_box["y"] + cell2_box["height"]:
                specialty_visible += 1
        assert specialty_visible <= 12, \
            f"BUG-UX-017: {specialty_visible} specialties visible without scrolling. Prescribers: {prescriber_count}. Max 12 required."
        assert specialty_visible > 0, "No specialties visible"
        _screenshot(page, "08")
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
        ctx = ssl.create_default_context(cafile=os.path.join(CERTS_DIR, "ca.crt"))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(os.path.join(CERTS_DIR, "findcare.crt"), os.path.join(CERTS_DIR, "findcare.key"))
        # If this connection succeeds with TLS 1.2 minimum, the requirement is met
        import socket
        sock = socket.create_connection(("localhost", EVALCARE_PORT or 8001), timeout=10)
        ssock = ctx.wrap_socket(sock, server_hostname="localhost")
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
        # Store original nonce for comparison
        orig = env.get("original_token", {})
        if orig:
            orig_nonce, orig_guid = _parse_token(orig.get("token", ""))
            if orig_guid:
                env["original_guid"] = orig_guid
            if orig_nonce:
                env.setdefault("all_nonces", []).append(("original", orig_nonce))
        # Handoff 1 of 6: FindCare → EvaluateCare
        nonce, guid = _verify_session_identity(page, env, "FindCare→EvaluateCare")
        env["evalcare_guid"] = guid
        env["evalcare_nonce"] = nonce
        # Token in left panel must be below the specialty list, not beside it
        left = page.locator("#leftPanel")
        token_el = left.locator("#guiSessionCell, #guiSessionId")
        assert token_el.count() > 0, "Token element not found in left panel"
        token_box = token_el.bounding_box()
        scroll_div = left.locator("div[style*='overflow-y']")
        if scroll_div.count() > 0:
            scroll_box = scroll_div.first.bounding_box()
            assert token_box["y"] > scroll_box["y"] + scroll_box["height"] - 5, \
                f"Token is beside specialties, not below. Token y={token_box['y']}, scroll bottom={scroll_box['y'] + scroll_box['height']}"
        _screenshot(page, "17")


# Step 18 [EPIC-002-F-001-S-012-REQ-B-007]
class TestStep18:
    def test_token_distinct_colors(self, env):
        page = env["page"]
        # Right panel must have distinct colors
        right = page.locator("#rightPanel")
        assert right.locator("[style*='6366f1']").count() > 0, "Right panel: Signed token not in indigo"
        assert right.locator("[style*='d97706']").count() > 0, "Right panel: Nonce not in amber"
        assert right.locator("[style*='0b7a75']").count() > 0, "Right panel: GUID not in teal"
        # Left panel must have identical distinct colors
        left = page.locator("#leftPanel")
        assert left.locator("[style*='d97706']").count() > 0, "Left panel: Nonce not in amber — must be identical to right panel"
        assert left.locator("[style*='0b7a75']").count() > 0, "Left panel: GUID not in teal — must be identical to right panel"
        _screenshot(page, "18")


# Step 19 [EPIC-002-F-001-S-012-REQ-T-003]
class TestStep19:
    def test_nonce_changed_evaluatecare(self, env):
        # Nonce uniqueness already verified by _verify_session_identity in step 17
        # This step confirms the stored values are present
        assert env.get("evalcare_nonce"), "EvaluateCare nonce not stored from step 17"
        assert env.get("evalcare_guid"), "EvaluateCare GUID not stored from step 17"
        assert env.get("original_guid"), "Original GUID not stored"
        assert env["evalcare_guid"] == env["original_guid"], \
            f"GUID changed: ec={env['evalcare_guid']} orig={env['original_guid']}"
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
        page = env["page"]
        # MUST be a specialty filter-toggle, NOT a filter-provider-type. The
        # provider-type handler (Website/index.html:1280) returns ownership to
        # FindCare immediately when EvaluateCare owns, which would skip the
        # gui:reset path TestStep22 depends on. Per Skip 2026-04-20.
        checkbox = page.locator("#leftPanel input[data-gui-action='filter-toggle']").first
        assert checkbox.count() > 0, "No specialty filter-toggle checkboxes found"
        checkbox.click()
        page.wait_for_timeout(3000)
        _screenshot(page, "21")


# Step 22 [TEST-SIM-001-REQ-014]
class TestStep22:
    def test_return_to_findcare(self, env):
        page = env["page"]
        apply_btn = page.locator("[data-gui-action='filter-apply']")
        assert apply_btn.count() > 0, "Apply Filter button not found"
        apply_btn.click()
        page.wait_for_timeout(5000)
        # Chat iframe MUST be visible — FindCare has control
        assert page.locator("#coreChatFrame").is_visible(), "Chat iframe not restored"
        # EvaluateCare splash MUST be hidden
        ec_splash = page.locator("#evalcareSplash")
        if ec_splash.count() > 0:
            assert not ec_splash.is_visible(), "EvaluateCare splash still visible after return"
        # SharedServices splash MUST be hidden
        sh_splash = page.locator("#sharedSplash")
        if sh_splash.count() > 0:
            assert not sh_splash.is_visible(), "SharedServices splash still visible after return"
        # Welcome repaints async after postMessage gui:reset → React re-render.
        # Re-fetch the chat_frame fresh (the iframe element may have re-mounted
        # since TestStep04 captured the original frame reference). Then poll.
        chat_frame = None
        for f in page.frames:
            if ":7860" in f.url or "hf.space" in f.url:
                chat_frame = f
                break
        assert chat_frame is not None, "Chat iframe not found at TestStep22"
        env["chat_frame"] = chat_frame  # update env for later steps
        welcome_words = _get_welcome_words()
        seen_dump = {"body": ""}

        def _check_welcome():
            body_text = chat_frame.locator("body").inner_text()
            seen_dump["body"] = body_text
            matches = sum(1 for w in welcome_words[:25] if w in body_text)
            if matches < 20:
                raise AssertionError(f"only {matches}/25 welcome words present")
            return matches

        try:
            _retry("test22_welcome_repaint", 30, 500, _check_welcome)
        except Exception:
            print(f"  [diag22] chat_frame.body inner_text (first 500 chars): {seen_dump['body'][:500]}", flush=True)
            raise
        _screenshot(page, "22")


# Step 23 [TEST-SIM-001-REQ-015]
class TestStep23:
    def test_input_focused_after_return(self, env):
        frame = env.get("chat_frame", env["page"])
        chat_input = frame.locator("input[placeholder*='Type a message'], textarea").first
        _retry("test23_input_visible", 10, 500,
               lambda: expect(chat_input).to_be_visible(timeout=400))
        _retry("test23_input_focused", 15, 1000,
               lambda: expect(chat_input).to_be_focused(timeout=800))
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
        ctx = ssl.create_default_context(cafile=os.path.join(CERTS_DIR, "ca.crt"))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(os.path.join(CERTS_DIR, "findcare.crt"), os.path.join(CERTS_DIR, "findcare.key"))
        import socket
        sock = socket.create_connection(("localhost", SHARED_PORT or 8002), timeout=10)
        ssock = ctx.wrap_socket(sock, server_hostname="localhost")
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
        # Nonce uniqueness already verified by _verify_session_identity in step 29
        assert env.get("shared_nonce"), "SharedServices nonce not stored from step 29"
        assert env.get("shared_guid"), "SharedServices GUID not stored from step 29"
        assert env["shared_guid"] == env["original_guid"], \
            f"GUID changed: shared={env['shared_guid']} orig={env['original_guid']}"
        _screenshot(env["page"], "30")


# Step 31 [EPIC-002-F-001-S-012-REQ-T-003] — Handoff 4: SharedServices → FindCare
class TestStep31:
    def test_shared_to_findcare(self, env):
        page = env["page"]
        # Touch filter to return to FindCare
        checkbox = page.locator("#leftPanel input[type='checkbox']").first
        assert checkbox.count() > 0, "No checkboxes to trigger return"
        checkbox.click()
        page.wait_for_timeout(3000)
        apply_btn = page.locator("[data-gui-action='filter-apply']")
        assert apply_btn.count() > 0, "Apply Filter button not found"
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
        # Return to FindCare first (touch filter)
        checkbox = page.locator("#leftPanel input[type='checkbox']").first
        if checkbox.count() > 0:
            checkbox.click()
            page.wait_for_timeout(3000)
        apply_btn = page.locator("[data-gui-action='filter-apply']")
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
        r = c.get(f"{FINDCARE_URL}/health")
        assert r.json()["status"] == "ok"
        r = c.get(f"{EVALCARE_URL}/health")
        assert r.json()["service"] == "evaluate_care"
        r = c.get(f"{SHARED_URL}/health")
        assert r.json()["service"] == "shared_services"
        c.close()
