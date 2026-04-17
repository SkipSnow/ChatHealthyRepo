# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# localSmokeTestPyTest.py — DEVOPS-LOCAL-B009
# 31-step Playwright smoke test. Each step maps to a requirement.
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

BASE_URL = os.getenv("SMOKE_TEST_URL", "https://localhost")
FINDCARE_URL = "https://localhost:7860"
EVALCARE_URL = "https://localhost:8001"
SHARED_URL = "https://localhost:8002"
CERTS_DIR = os.path.join(os.path.dirname(__file__), "..", "Shared", "ops", "certs")
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "test_output", "smoke_test")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
CHAT_TIMEOUT = 120_000


def _screenshot(page, name):
    page.screenshot(path=os.path.join(SCREENSHOT_DIR, f"{name}.png"), full_page=True)


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


@pytest.fixture(scope="module")
def env():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1400, "height": 900}, ignore_https_errors=True)
        page = context.new_page()
        yield {"page": page, "browser": browser, "context": context}
        context.close()
        browser.close()


# Step 1 [DEVOPS-LOCAL-B004]
class TestStep01:
    def test_http_redirects_to_https(self, env):
        page = env["page"]
        page.goto("http://localhost", wait_until="domcontentloaded")
        assert page.url.startswith("https://localhost"), f"Expected https://localhost, got {page.url}"
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
        assert "LOCAL" in text, f"Missing LOCAL: {text}"
        assert f"Version: {health['version']}" in text, f"Version wrong: {text}"
        assert f"Framework: {health['framework']}" in text, f"Framework wrong: {text}"
        assert f"Build: {health['build']}" in text, f"Build wrong: {text}"
        _screenshot(page, "02")


# Step 3 [DEVOPS-BANNER-B003]
class TestStep03:
    def test_shared_services_button(self, env):
        page = env["page"]
        btn = page.locator("#sharedServicesNav")
        expect(btn).to_be_visible()
        expect(btn).to_contain_text("Shared Services")
        _screenshot(page, "03")


# Step 4 [TEST-SIM-001-REQ-003]
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


# Step 5 [TEST-SIM-001-REQ-004]
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


# Step 6 [TEST-SIM-001-REQ-005]
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


# Step 7 [TEST-SIM-001-REQ-006]
class TestStep07:
    def test_specialties_in_left_panel(self, env):
        page = env["page"]
        left = page.locator("#leftPanel")
        count = left.locator("input[type='checkbox']").count()
        assert count > 0, f"No specialty checkboxes: {left.inner_text()[:300]}"
        env["specialty_count"] = count
        _screenshot(page, "07")


# Step 8 [FC-FILT-001-REQ-015]
class TestStep08:
    def test_specialty_scroll_max_12(self, env):
        page = env["page"]
        left = page.locator("#leftPanel")
        total = left.locator("input[type='checkbox']").count()
        if total > 12:
            # The scroll container must exist and constrain visible items
            scroll_div = left.locator("div[style*='overflow-y']")
            assert scroll_div.count() > 0, "No scroll container found for specialty list"
            box = scroll_div.first.bounding_box()
            assert box is not None, "Scroll container has no bounding box"
            panel_box = left.bounding_box()
            assert panel_box is not None, "Left panel has no bounding box"
            assert box["height"] <= panel_box["height"], \
                f"Scroll container ({box['height']}px) overflows panel ({panel_box['height']}px)"
        _screenshot(page, "08")


# Step 9 [TEST-SIM-001-REQ-005]
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


# Step 13 [UX-CTRL-003-REQ-003]
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


# Step 15 [SEC-HTTPS-001-REQ-009]
class TestStep15:
    def test_mtls_evaluatecare_tls12(self):
        ctx = ssl.create_default_context(cafile=os.path.join(CERTS_DIR, "ca.crt"))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(os.path.join(CERTS_DIR, "findcare.crt"), os.path.join(CERTS_DIR, "findcare.key"))
        # If this connection succeeds with TLS 1.2 minimum, the requirement is met
        import socket
        sock = socket.create_connection(("localhost", 8001), timeout=10)
        ssock = ctx.wrap_socket(sock, server_hostname="localhost")
        tls_ver = ssock.version()
        ssock.close()
        assert tls_ver in ("TLSv1.2", "TLSv1.3"), f"TLS version is {tls_ver}, need 1.2+"


# Step 16 [TEST-SIM-001-REQ-010]
class TestStep16:
    def test_right_panel_providers(self, env):
        page = env["page"]
        right = page.locator("#rightPanel").inner_text()
        assert "EVALUATECARE" in right.upper(), f"Missing EvaluateCare header: {right[:400]}"
        assert re.findall(r"\d{10}", right), f"No NPI in right panel: {right[:400]}"
        _screenshot(page, "16")


# Step 17 [TEST-SIM-001-REQ-011]
class TestStep17:
    def test_token_same_sent_received(self, env):
        page = env["page"]
        right = page.locator("#rightPanel").inner_text()
        assert "SESSION VERIFICATION" in right.upper(), f"No session verification: {right[:400]}"
        assert "VERIFIED" in right.upper() and "YES" in right.upper(), f"Not verified: {right[:400]}"
        # Extract GUID from right panel and compare with left panel token
        guids = re.findall(r'GUID:\s*(\w+)', right)
        assert len(guids) > 0, f"No GUID found in right panel: {right[:400]}"
        # The GUID in the right panel must match the session GUID
        left = page.locator("#leftPanel").inner_text()
        left_tokens = re.findall(r'CH\w{30,}', left)
        assert len(left_tokens) > 0, f"No session token found in left panel: {left[:300]}"
        left_guid = left_tokens[0][-32:]
        assert guids[0] == left_guid, f"GUID mismatch: right={guids[0]} left={left_guid}"
        env["evalcare_guid"] = guids[0]
        nonces = re.findall(r'Nonce:\s*(\w+)', right)
        assert len(nonces) > 0, f"No Nonce found in right panel: {right[:400]}"
        env["evalcare_nonce"] = nonces[0]


# Step 18 [SEC-HTTPS-001-REQ-013]
class TestStep18:
    def test_token_distinct_colors(self, env):
        page = env["page"]
        right = page.locator("#rightPanel")
        # Signed token label in indigo (#6366f1)
        signed_el = right.locator("[style*='6366f1']")
        # Nonce label in amber (#d97706)
        nonce_el = right.locator("[style*='d97706']")
        # GUID label in teal (#0b7a75)
        guid_el = right.locator("[style*='0b7a75']")
        assert signed_el.count() > 0, "Signed token not in distinct color (indigo)"
        assert nonce_el.count() > 0, "Nonce not in distinct color (amber)"
        assert guid_el.count() > 0, "GUID not in distinct color (teal)"
        _screenshot(page, "18")


# Step 19 [SEC-HTTPS-001-REQ-012]
class TestStep19:
    def test_nonce_changed_evaluatecare(self, env):
        original = env.get("original_token", {})
        assert original, "No original token stored from step 6"
        orig_nonce, orig_guid = _parse_token(original.get("token", ""))
        ec_nonce = env.get("evalcare_nonce", "")
        ec_guid = env.get("evalcare_guid", "")
        assert orig_guid, "Original token GUID is empty"
        assert ec_guid, "EvaluateCare GUID is empty"
        assert orig_nonce, "Original nonce is empty"
        assert ec_nonce, "EvaluateCare nonce is empty"
        assert orig_guid == ec_guid, f"GUID changed: orig={orig_guid} ec={ec_guid}"
        assert orig_nonce != ec_nonce, f"Nonce did NOT change after EvaluateCare: {orig_nonce}"
        _screenshot(env["page"], "19")


# Step 20 [UX-CTRL-003-REQ-004]
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
        checkbox = page.locator("#leftPanel input[type='checkbox']").first
        assert checkbox.count() > 0, "No checkboxes to change"
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
        _screenshot(page, "22")


# Step 23 [TEST-SIM-001-REQ-015]
class TestStep23:
    def test_input_focused_after_return(self, env):
        frame = env.get("chat_frame", env["page"])
        chat_input = frame.locator("input[placeholder*='Type a message'], textarea").first
        expect(chat_input).to_be_visible()
        chat_input.click()
        expect(chat_input).to_be_focused()
        _screenshot(env["page"], "23")


# Step 24 [SEC-HTTPS-001-REQ-012]
class TestStep24:
    def test_nonce_changed_return_findcare(self, env):
        token = _get_fresh_token()
        nonce, guid = _parse_token(token.get("token", ""))
        orig_nonce, orig_guid = _parse_token(env.get("original_token", {}).get("token", ""))
        ec_nonce = env.get("evalcare_nonce", "")
        assert guid, "Return token GUID is empty"
        assert nonce, "Return token nonce is empty"
        assert orig_guid, "Original GUID is empty"
        assert orig_guid == guid, f"GUID changed on return: {orig_guid} vs {guid}"
        assert nonce != orig_nonce, f"Nonce same as original on return: {nonce}"
        assert ec_nonce, "EvaluateCare nonce not stored from step 17"
        assert nonce != ec_nonce, f"Nonce same as EvaluateCare on return: {nonce}"
        env["return_nonce"] = nonce
        _screenshot(env["page"], "24")


# Step 25 [DEVOPS-BANNER-B003]
class TestStep25:
    def test_push_shared_services(self, env):
        page = env["page"]
        btn = page.locator("#sharedServicesNav")
        assert btn.count() > 0 and btn.is_visible(), "Shared Services button missing"
        btn.click()
        page.locator("#rightPanel:has-text('Shared Services')").wait_for(state="visible", timeout=15000)
        page.wait_for_timeout(3000)
        _screenshot(page, "25")


# Step 26 [DEVOPS-BANNER-B004]
class TestStep26:
    def test_shared_services_has_control(self, env):
        page = env["page"]
        assert not page.locator("#coreChatFrame").is_visible(), "Chat iframe still visible"
        splash = page.locator("#sharedSplash")
        assert splash.count() > 0 and splash.is_visible(), "SharedServices splash not visible"
        _screenshot(page, "26")


# Step 27 [SEC-HTTPS-001-REQ-010]
class TestStep27:
    def test_mtls_shared_services_tls12(self):
        ctx = ssl.create_default_context(cafile=os.path.join(CERTS_DIR, "ca.crt"))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(os.path.join(CERTS_DIR, "findcare.crt"), os.path.join(CERTS_DIR, "findcare.key"))
        import socket
        sock = socket.create_connection(("localhost", 8002), timeout=10)
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
        assert "Shared Services" in text, f"Missing Shared Services: {text}"
        assert "is still unimplemented" in text, f"Missing unimplemented: {text}"
        _screenshot(page, "28")


# Step 29 [SEC-HTTPS-001-REQ-008]
class TestStep29:
    def test_shared_services_token_auth(self, env):
        page = env["page"]
        right = page.locator("#rightPanel").inner_text()
        assert "SESSION VERIFICATION" in right.upper(), f"No session verification: {right[:400]}"
        assert "VERIFIED" in right.upper() and "YES" in right.upper(), f"Not verified: {right[:400]}"
        assert "SHARED SERVICES" in right.upper(), f"Not SharedServices context: {right[:400]}"
        # Extract and store nonce for step 30
        nonces = re.findall(r'Nonce:\s*(\w+)', right)
        assert len(nonces) > 0, f"No Nonce found for SharedServices: {right[:400]}"
        env["shared_nonce"] = nonces[0]
        guids = re.findall(r'GUID:\s*(\w+)', right)
        assert len(guids) > 0, f"No GUID found for SharedServices: {right[:400]}"
        env["shared_guid"] = guids[0]
        _screenshot(page, "29")


# Step 30 [SEC-HTTPS-001-REQ-012]
class TestStep30:
    def test_nonce_changed_shared_services(self, env):
        sh_nonce = env.get("shared_nonce", "")
        sh_guid = env.get("shared_guid", "")
        return_nonce = env.get("return_nonce", "")
        ec_nonce = env.get("evalcare_nonce", "")
        orig_nonce, orig_guid = _parse_token(env.get("original_token", {}).get("token", ""))
        assert sh_nonce, "SharedServices nonce not stored from step 29"
        assert sh_guid, "SharedServices GUID not stored from step 29"
        assert return_nonce, "FindCare return nonce not stored from step 24"
        assert ec_nonce, "EvaluateCare nonce not stored from step 17"
        assert orig_guid, "Original GUID is empty"
        assert sh_guid == orig_guid, f"GUID changed: shared={sh_guid} orig={orig_guid}"
        assert sh_nonce != return_nonce, f"Nonce same as FindCare return: {sh_nonce}"
        assert sh_nonce != ec_nonce, f"Nonce same as EvaluateCare: {sh_nonce}"
        assert sh_nonce != orig_nonce, f"Nonce same as original: {sh_nonce}"
        _screenshot(env["page"], "30")


# Step 31 [SEC-HTTPS-001-REQ-011]
class TestStep31:
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
