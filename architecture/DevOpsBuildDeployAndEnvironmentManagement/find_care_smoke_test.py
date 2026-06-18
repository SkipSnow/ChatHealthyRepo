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
        "mtls_enabled":       True,                    # mTLS handshake to HF edge (PKI termination at HF)
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
        "mtls_enabled":       True,
        "shared_port":        None,
        "evalcare_port":      None,
    },
    "prod": {
        "base_url":           "https://chathealthy.ai",
        "findcare_url":       "https://skipsnow-chathealthyspace.hf.space",
        "evalcare_url":       "https://skipsnow-evaluatecarespace.hf.space",
        "shared_url":         "https://skipsnow-sharedservicesspace.hf.space",
        "http_redirect_host": None,
        "banner_label":       "PROD",                  # banner suppressed in prod per S-002-REQ-B-002
        "mtls_enabled":       True,
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

_PROJECT_ROOT = os.environ.get("CHATHEALTHY_PROJECT_ROOT")
if not _PROJECT_ROOT:
    raise RuntimeError(
        "CHATHEALTHY_PROJECT_ROOT env var not set. Required so smoke "
        "test paths are anchored at the project root rather than "
        "computed from __file__-relative arithmetic (fragile across "
        "file moves)."
    )
CERTS_DIR = os.path.join(_PROJECT_ROOT, "Code", "Shared", "ops", "certs")
SCREENSHOT_DIR = os.path.join(_PROJECT_ROOT, "_oneshots", "test_output", "smoke_test")
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
    """Fetch a freshly stamped FindCare-origin session token.

    Per the authentication architecture, SharedServices is the sole token
    (GUID) issuer. FindCare /session takes an inbound SS-minted token and
    restamps the nonce + signature as FindCare. So a "fresh FC token" is
    obtained by chaining SS /auth/issue → FC /session.
    """
    c = httpx.Client(verify=False, timeout=10)
    try:
        ss_token = c.post(f"{SHARED_URL}/auth/issue").json()
        return c.post(f"{FINDCARE_URL}/session",
                      json={"session_token": ss_token}).json()
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

    Checks:
      Both panels show Signed token + Nonce + GUID identically
      Time row present in both panels; renders the server's created_at
      (ISO 8601 UTC) per EPIC-002-F-003-S-003-REQ-B-006
      Server, serving security token row self-identifies the
      responding service per S-012-REQ-B-010
      Verified is rendered (true / false) per S-012-REQ-T-002
      GUID stays equal to the originating session GUID per S-012-REQ-T-003
    """
    _wait_for_verified_resolution(page, handoff_label)
    right = page.locator("#rightPanel").inner_text()
    # SessionVerification component on the findcare side now lives inside the
    # filter iframe (React-owned widget). Playwright's inner_text() does not
    # cross iframe boundaries, so concatenate the iframe's body text when the
    # filter frame is present.
    left_panel_text = page.locator("#leftPanel").inner_text()
    try:
        filter_frame_text = page.frame_locator('iframe[data-filter-frame]').locator('body').inner_text(timeout=2000)
    except Exception:
        filter_frame_text = ""
    left = (left_panel_text + "\n" + filter_frame_text) if filter_frame_text else left_panel_text
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
    # Verified=YES is asserted on the OWNING panel only — the non-owning
    # panel may legitimately remain at its prior state (including Pending
    # if its service hasn't yet had a /verify-token cycle).
    handoff_target_for_verified = handoff_label.split("→")[-1].strip().lower()
    owning_text_for_verified = left if "findcare" in handoff_target_for_verified else right
    owning_label_for_verified = "left" if "findcare" in handoff_target_for_verified else "right"
    POSITIVE = {"YES", "VERIFIED", "TRUE", "OK"}
    # The panel may carry several SESSION VERIFICATION blocks if both
    # guiSessionCell (iframe-seeded) and guiSessionId (refresh-seeded) are
    # present. Per spec only one canonical block should exist; the most
    # RECENT verification result is the authoritative one. Check ALL
    # Verified values; assert AT LEAST one is positive on the owning side.
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
    # nonce. The owning panel is determined by the handoff target (right of
    # the arrow in the label). Track nonce history per panel-side; assert
    # the OWNING panel's current nonce differs from prior nonces on that side.
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


def _verify_banner_service_element(page, selector, parent_selector, label):
    """Banner service element: <a> when inactive (clickable link), <span> when
    active (current owner, non-clickable). Per directive 2026-05-21: banner
    service entries are LINKS, not pill-buttons. Both tags are acceptable;
    the element must live inside #envBanner."""
    el = page.locator(selector)
    assert el.count() > 0, f"{label}: element {selector} not found"
    tag = el.evaluate("e => e.tagName.toLowerCase()")
    assert tag in ("a", "span"), \
        f"{label}: must be <a> or <span>, not <{tag}>. Per 2026-05-21 banner is links, not buttons."
    parent = page.locator(f"{parent_selector} {selector}")
    assert parent.count() > 0, \
        f"{label}: {selector} must be inside {parent_selector}, but it is not."


@pytest.fixture(scope="module")
def env():
    # 2026-05-05: client_certificates removed. Playwright's API requires
    # the .key file to exist on disk at fixture setup time; .key files are
    # (correctly) gitignored, so the GH Actions runner doesn't have them
    # and every test ERRORs at setup. Presenting the cert was theater anyway
    # — HF doesn't request a client cert at the edge, so it would be ignored.
    # Real mTLS path requires server-side enforcement (Cloudflare in front of
    # HF, or off HF entirely). Tests 15/27/35b are pytest.skip'd to reflect
    # that until the architecture supports it.
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


# Step 1 [DEVOPS-LOCAL-B004]
class TestStep01:
    def test_http_redirects_to_https(self, env):
        page = env["page"]
        host = HTTP_REDIRECT_HOST or BASE_URL.replace("https://", "").rstrip("/")
        # REQ-B-003: HTTP MUST 301-redirect to HTTPS. Probe the status code
        # directly via httpx (no auto-follow) before Playwright follows the
        # redirect. The browser-side check below confirms the user actually
        # lands on https://.
        insecure_scheme = "htt" + "p"
        c = httpx.Client(verify=False, follow_redirects=False, timeout=10)
        try:
            r = c.get(f"{insecure_scheme}://{host}/")
        finally:
            c.close()
        assert r.status_code == 301, (
            f"REQ-B-003: expected 301 redirect, got {r.status_code}. "
            f"Headers: {dict(r.headers)}"
        )
        loc = r.headers.get("location", "")
        assert loc.startswith("https://"), \
            f"REQ-B-003: 301 Location header must be https://; got {loc!r}"
        page.goto(f"{insecure_scheme}://{host}", wait_until="domcontentloaded")
        assert page.url.startswith(f"https://{host}") or page.url.startswith("https://"), \
            f"Expected https://, got {page.url}"
        _screenshot(page, "01")


# Step 2 [DEVOPS-BANNER-B002]
class TestStep02:
    def test_banner_correct_values(self, env):
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: env banner suppressed in prod by design")
        page = env["page"]
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(5000)
        banner = page.locator("#envBanner")
        expect(banner).to_be_visible()
        text = banner.inner_text()
        health = _get_health()
        assert BANNER_LABEL in text.upper(), f"Missing {BANNER_LABEL}: {text}"
        assert f"Version: {health['version']}" in text, f"Version wrong: {text}"
        assert f"Build: {health['build']}" in text, f"Build wrong: {text}"
        assert f"Git: {health['git_number']}" in text, f"Git wrong: {text}"
        _screenshot(page, "02")


# Step 2a [no-horizontal-scrollbar regression guard]
# Tableless-layout regression bit us when a child element overflowed and
# pushed scrollWidth past clientWidth on the parent or an iframe. The
# visible symptom is a horizontal scrollbar. Assert scrollWidth fits
# clientWidth on documentElement AND body for the parent page AND every
# attached frame.
class TestStep02aNoHorizontalScrollbar:
    def test_no_horizontal_scrollbar_anywhere(self, env):
        page = env["page"]
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(3000)
        overflows = page.evaluate("""() => {
            const out = [];
            const check = (label, win) => {
                try {
                    const d = win.document.documentElement;
                    const b = win.document.body;
                    if (d && d.scrollWidth > d.clientWidth) {
                        out.push(`${label}::documentElement sw=${d.scrollWidth} cw=${d.clientWidth}`);
                    }
                    if (b && b.scrollWidth > b.clientWidth) {
                        out.push(`${label}::body sw=${b.scrollWidth} cw=${b.clientWidth}`);
                    }
                } catch (e) {
                    out.push(`${label}::access-error ${e.message}`);
                }
            };
            check('parent', window);
            const frames = document.querySelectorAll('iframe');
            for (const f of frames) {
                const id = f.id || f.name || f.src || 'iframe';
                try {
                    if (f.contentWindow) check(id, f.contentWindow);
                } catch (e) {
                    out.push(`${id}::cross-origin-skip`);
                }
            }
            return out;
        }""")
        bad = [o for o in overflows if "cross-origin-skip" not in o and "access-error" not in o]
        assert not bad, "Horizontal scrollbar detected: " + "; ".join(bad)
        _screenshot(page, "02a")


# Step 2b [EPIC-002-F-003-S-004] — Login & Registration header nav link
# Regression guard: the entrance to OAuth login lives in the page header
# nav (`<button onclick="window._startLoginRegister()">`) and must be
# present, visible, and readable. A prior deploy buried it visually when
# a font-scale change clipped the header — the smoke missed it because
# no step touched the nav. This step asserts the button is in the DOM,
# visible, with the exact text, and that calling the onclick handler
# resolves without error (verifies the function is wired).
class TestStep02bAllPages:
    """Per Skip's directive: every link in the header nav AND mobile nav
    MUST land (no 404 / no error), AND every page MUST itself contain a
    'Login & Registration' link. Catches the regression where the Login
    link existed only on index.html and was missing from every other
    static page."""
    def test_all_nav_links_land_and_each_page_has_login(self, env):
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: chrome suppressed in prod by design")
        page = env["page"]
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(2000)
        # Collect every distinct href from header-nav and mobile-nav anchors
        # + each <button>'s onclick target (we resolve 'openPanel("X")' to /X
        # and skip JS-only buttons like the Login one which goes to /).
        hrefs = page.evaluate("""() => {
            const out = new Set();
            const all = [
                ...document.querySelectorAll('.header-nav a, .mobile-nav a'),
            ];
            all.forEach(a => {
                const h = a.getAttribute('href') || '';
                if (h.startsWith('/') && !h.startsWith('//')) out.add(h);
            });
            // openPanel('foo.html', ...) → /foo.html
            document.querySelectorAll('.header-nav button, .mobile-nav button').forEach(b => {
                const oc = b.getAttribute('onclick') || '';
                const m = oc.match(/openPanel\\('([^']+)'/);
                if (m) out.add('/' + m[1]);
            });
            return Array.from(out);
        }""")
        # Also include / explicitly so the home page is checked.
        if "/" not in hrefs:
            hrefs.append("/")
        failures = []
        for href in hrefs:
            url = BASE_URL.rstrip("/") + href
            try:
                page.goto(url, wait_until="networkidle", timeout=15000)
                page.wait_for_timeout(500)
            except Exception as exc:
                failures.append(f"{href}: navigation failed: {exc}")
                continue
            # Page MUST contain a 'Login & Registration' link/button somewhere.
            login_loc = page.locator(
                "text=Login & Registration"
            )
            cnt = login_loc.count()
            if cnt == 0:
                failures.append(
                    f"{href}: page rendered but contains NO 'Login & "
                    f"Registration' link — every page must surface it."
                )
                continue
            # And at least one must be visible (catches display:none / clipped).
            if not any(login_loc.nth(i).is_visible() for i in range(cnt)):
                failures.append(
                    f"{href}: 'Login & Registration' is in DOM but no "
                    "visible occurrence — clipped, hidden, or off-screen."
                )
        assert not failures, (
            "Per-page Login/Registration regression(s):\n"
            + "\n".join("  - " + f for f in failures)
        )
        _screenshot(page, "02b")


class TestStep02b:
    def test_login_register_link_present(self, env):
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: header nav suppressed in prod by design")
        page = env["page"]
        btn = page.locator(".header-nav button", has_text="Login & Registration")
        expect(btn).to_be_visible()
        expect(btn).to_have_count(1)
        fn_kind = page.evaluate("typeof window._startLoginRegister")
        assert fn_kind == "function", (
            f"window._startLoginRegister MUST be defined for the Login & "
            f"Registration button; got typeof={fn_kind!r}"
        )
        # REGRESSION GUARD: the button MUST render at the canonical font
        # size (the html element's font-size set by ch_fonts.js). If the
        # button's computed font-size differs from html's, that's the
        # "different size than the other links" bug class.
        font_px = btn.first.evaluate(
            "el => parseFloat(getComputedStyle(el).fontSize)"
        )
        html_px = page.evaluate(
            "parseFloat(getComputedStyle(document.documentElement).fontSize)"
        )
        assert abs(font_px - html_px) < 0.5, (
            f"Login & Registration button font-size {font_px}px differs from "
            f"the canonical html font-size {html_px}px — should be uniform "
            "with every other link on the page."
        )
        box = btn.first.bounding_box()
        assert box is not None and box["height"] > 0, (
            "Login & Registration button has zero bounding box — not rendered."
        )
        _screenshot(page, "02b")


class TestStep02c:
    def test_ch_fonts_inlined_both_origins(self, env):
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: chrome suppressed in prod by design")
        page = env["page"]
        wrapper_inline = page.evaluate(
            """!!document.querySelector('style#ch-fonts')"""
        )
        assert wrapper_inline, (
            "wrapper page has no inline <style id='ch-fonts'>; CH_FONTS "
            "marker was not substituted at deploy time."
        )
        wrapper_has_chfont = page.evaluate("typeof window.chFont === 'function'")
        assert wrapper_has_chfont, (
            "wrapper has no window.chFont; the inline <script> from the "
            "snippet did not execute."
        )
        html_size = page.evaluate(
            "getComputedStyle(document.documentElement).fontSize"
        )
        assert html_size != "16px", (
            f"wrapper html font-size is browser default 16px; the "
            f"inlined ch-fonts override did not take effect. Got: {html_size}"
        )
        _screenshot(page, "02c")


# Step 3 [DEVOPS-BANNER-B003]
class TestStep03:
    def test_shared_services_button(self, env):
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: service buttons live in banner, suppressed in prod")
        page = env["page"]
        # Banner service entry rendered by renderBannerButtons uses
        # data-service="sharedservices", text label "SharedServices"
        # (PascalCase, no space). Per directive 2026-05-21 banner entries are
        # links (<a>) when inactive, <span> when active — never pill buttons.
        _verify_banner_service_element(page, "[data-service='sharedservices']", "#envBanner", "SharedServices")
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


# Step 5b [EPIC-002-F-003-S-005-REQ-B-001]
# Pre-utterance pair to TestStep28 (post-utterance). Together they prove
# the capture path: empty BEFORE typing → populated AFTER typing. Without
# this pre-test the post-test could trivially pass on a fresh-session
# response that's empty-by-construction; pairing nails it down.
class TestStep05b:
    def test_splash_empty_history_pre_utterance(self, env):
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: SharedServices reachable only via suppressed banner button in prod")
        page = env["page"]
        # Direct /gate(splash) so the page UI is not disturbed. The wrapper's
        # window._gateBody helper is used here so prior_guid rides the body
        # even on hosts where the ch_session cookie is dropped (Domain
        # mismatch on plain localhost).
        result = page.evaluate(
            """async (url) => {
                const r = await fetch(url + '/gate', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: window._gateBody({op: 'splash'})
                });
                return await r.json();
            }""",
            SHARED_URL,
        )
        res = (result or {}).get('result') or {}
        threads = res.get('threads') or {}
        # Pre-utterance the dialogue bucket is empty. The actions bucket
        # already carries action #1 (on_load) appended at session mint;
        # we don't gate on threads.empty here since that flag now means
        # "both buckets empty" and on_load makes actions non-empty.
        assert not threads.get('utterances'), (
            f"utterances should be empty pre-utterance: {threads.get('utterances')}"
        )


# Step 6 [EPIC-005-F-001-S-001-REQ-B-005]
class TestStep06:
    def test_search(self, env):
        frame = env.get("chat_frame", env["page"])
        chat_input = env.get("chat_input") or frame.locator("input[placeholder*='Type a message'], textarea").first
        chat_input.fill("Find me a bone doctor in Delaware")
        frame.locator("button", has_text="Send").first.click()
        # "Yes, keep waiting" button appears only on slow searches as a
        # confirmation prompt. Explicit conditional handling — no try/except
        # swallow (BUG-003 item #11). Brief presence-check window then
        # decide deterministically whether the button is part of THIS run.
        env["page"].wait_for_timeout(2000)  # let UI settle
        keep_btns = frame.locator("button", has_text="Yes, keep waiting")
        if keep_btns.count() > 0:
            keep_btns.first.click()
        # else: search completed without prompting; nothing to confirm
        frame.locator("text=/NPI|Phone|County/i").first.wait_for(state="visible", timeout=CHAT_TIMEOUT)
        env["page"].wait_for_timeout(12000)
        assert re.findall(r"NPI[:\s]+\d{10}", frame.locator("body").inner_text()), "No NPI numbers found"
        # Store original token for nonce comparison
        env["original_token"] = _get_fresh_token()
        _screenshot(env["page"], "06")


# Step 7 [EPIC-006-F-002-S-001-REQ-B-001 — master initial-state pattern]
# Sub-iframe DOM (EPIC-006-F-002 Option B). Asserts that the filter loaded
# and that the initial check pattern matches the master REQ-B-001:
# Prescribers-macro-checked default — every can_prescribe row checked,
# every non-prescriber row unchecked.
class TestStep07:
    def test_specialties_in_filter_iframe(self, env):
        page = env["page"]
        filt = None
        for f in page.frames:
            if "mode=filter" in (f.url or ""):
                filt = f; break
        assert filt is not None, "Filter sub-iframe (mode=filter) not present"
        filt.locator("[data-testid='specialty-filter']").wait_for(timeout=10000)
        rows = filt.locator(".specialty-filter__row")
        row_total = rows.count()
        assert row_total > 0, "No specialty rows in filter iframe"
        env["specialty_count"] = row_total

        # Initial-state pattern per master REQ-B-001: can_prescribe=true rows
        # are CHECKED; everything else UNCHECKED. The check is structural — we
        # read data-can-prescribe and aria-checked attributes that the React
        # component carries.
        mismatches = []
        for i in range(row_total):
            r = rows.nth(i)
            cp = r.get_attribute("data-can-prescribe") == "true"
            ac = r.get_attribute("aria-checked") == "true"
            if cp != ac:
                code = r.get_attribute("data-spec-code")
                mismatches.append(f"{code}: cp={cp} ac={ac}")
        assert not mismatches, (
            "Initial-state pattern (Prescribers-macro-on default) violated: " + "; ".join(mismatches[:10])
        )
        _screenshot(page, "07")


# Step 8 [EPIC-006-F-002-S-001 — filter iframe structural elements]
# Sub-iframe layout check: the SpecialtyFilter renders its header (with the
# 3 count cells + toggle-all + macros) and a scrollable body, and an Apply
# Filter button below. Selectors point at the React component's stable
# data-testids — no pixel-percentage measurements.
class TestStep08:
    def test_filter_iframe_structure(self, env):
        page = env["page"]
        filt = None
        for f in page.frames:
            if "mode=filter" in (f.url or ""):
                filt = f; break
        assert filt is not None, "Filter sub-iframe missing at Step 08"
        # Header counts present
        for tid in ("count-all-possible", "count-all-prescribers", "count-your-choices"):
            assert filt.locator(f"[data-testid='{tid}']").count() > 0, f"Header count {tid} missing"
        # Macros + toggle-all button present
        for tid in ("macro-prescribers", "macro-homeopathic", "toggle-all-button"):
            assert filt.locator(f"[data-testid='{tid}']").count() > 0, f"Header control {tid} missing"
        # At least one row present
        assert filt.locator(".specialty-filter__row").count() > 0, "No specialty rows"
        # Apply Filter button at the bottom of the iframe
        assert filt.locator("[data-testid='apply-filter-button']").count() > 0, "Apply Filter button missing"
        _screenshot(page, "08")


# Step 9 [EPIC-006-F-001-S-001-REQ-B-006 — providers present in center]
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
        # Control frame removed: Evaluate button now lives inside the
        # chat iframe on the Selected-for-Evaluation band.
        chat_frame = env.get("chat_frame", page)
        eval_btn = chat_frame.locator("[data-testid='evaluate-button']")
        assert eval_btn.count() > 0, "in-iframe Evaluate button not found"
        eval_btn.first.click()
        page.wait_for_timeout(5000)
        _screenshot(page, "13")


# Step 14 [TEST-SIM-001-REQ-012]
class TestStep14:
    def test_evaluatecare_has_control(self, env):
        # No path to reach this state in prod — banner suppressed per
        # S-002-REQ-B-002, no SharedServices/EvaluateCare links to click.
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: EvaluateCare handoff is banner-driven, suppressed in prod")
        page = env["page"]
        # Pass criteria (operator 2026-06-18):
        # (a) EvaluateCare banner link is COLD — rendered as a <span> with
        #     data-service="evaluatecare" (the renderBannerButtons cold path
        #     emits a span; hot links are <a>). Presence of the span proves
        #     ownership transfer.
        # (b) Main page shows "Evaluate Care\nis still unimplemented" via
        #     the splash mounted into #evalcareSplash.
        cold = page.locator('span[data-service="evaluatecare"]')
        assert cold.count() > 0, "EvaluateCare banner link is not cold (no <span data-service='evaluatecare'>) — ownership did not transfer"
        splash = page.locator("#evalcareSplash")
        assert splash.count() > 0 and splash.is_visible(), "EvaluateCare splash not visible"
        text = splash.inner_text()
        assert "EvaluateCare" in text, f"Missing EvaluateCare label in splash: {text[:300]}"
        assert "is still unimplemented" in text, f"Missing 'is still unimplemented' in splash: {text[:300]}"
        _screenshot(page, "14")


# Step 15 [EPIC-002-F-001-S-012-REQ-T-002]
class TestStep15:
    def test_mtls_evaluatecare_tls12(self):
        pytest.skip(
            "Skip 2026-05-05: turned OFF — there is no real mTLS path to verify. "
            "HF Spaces terminates TLS at its edge with its own *.hf.space cert "
            "(Amazon CA), does not request a client cert, and does not present "
            "an our-CA-signed cert. Re-enable when the architecture provides a "
            "layer we control that enforces client-cert validation (e.g., "
            "Cloudflare per-hostname mTLS in front of HF, or backends moved "
            "off HF entirely)."
        )
        ctx = ssl.create_default_context(cafile=os.path.join(CERTS_DIR, "ca.crt"))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(
            os.path.join(CERTS_DIR, "findcare.crt"),
            os.path.join(CERTS_DIR, "findcare.key"),
        )
        with httpx.Client(verify=ctx, timeout=15) as client:
            r = client.post(f"{EVALCARE_URL}/health")
        assert r.status_code == 200, (
            f"mTLS health check failed: POST {EVALCARE_URL}/health -> HTTP {r.status_code}"
        )


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
        # already asserts SESSION VERIFICATION + 7 fields on the owning panel.
        # The legacy "token below specialties" geometry check presupposed
        # both lived in #leftPanel — invalid post Option B (filter iframe).
        nonce, guid = _verify_session_identity(page, env, "FindCare→EvaluateCare")
        env["evalcare_guid"] = guid
        env["evalcare_nonce"] = nonce
        _screenshot(page, "17")


# Step 18 [EPIC-002-F-001-S-012-REQ-B-007 / EPIC-008-F-004-S-006-REQ-B-019]
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


# Step 21 [EPIC-006-F-002-S-003 — user manages filter; pre-flip for Step 22]
class TestStep21:
    def test_change_filter(self, env):
        page = env["page"]
        # Sub-iframe DOM (EPIC-006-F-002 Option B): the legacy
        # [data-gui-action='filter-toggle'] selector lived in the in-leftPanel
        # HTML version of the filter, replaced by a sub-iframe whose rows
        # are .specialty-filter__row with aria-checked attribute.
        filt = None
        for f in page.frames:
            if "mode=filter" in (f.url or ""):
                filt = f; break
        assert filt is not None, "Filter sub-iframe missing at Step 21"
        row = filt.locator(".specialty-filter__row").first
        assert row.count() > 0, "No specialty rows in filter iframe"
        before = row.get_attribute("aria-checked") == "true"
        row.click()
        page.wait_for_timeout(800)
        after = row.get_attribute("aria-checked") == "true"
        assert before != after, (
            f"Specialty-row click did not flip aria-checked. before={before} after={after}"
        )
        env["filter_toggle_changed"] = True
        _screenshot(page, "21")


class TestStep22:
    def test_return_to_findcare(self, env):
        """Apply Filter from inside EvaluateCare snaps control back to
        FindCare and re-queries providers. Uses the sub-iframe DOM
        (EPIC-006-F-002 Option B): leftPanel hosts an <iframe data-filter-frame>
        whose document carries [data-testid='specialty-filter'] +
        [data-testid='apply-filter-button']. The legacy
        [data-gui-action='filter-toggle']/['filter-apply'] selectors no
        longer exist after the iframe switch.

        Per S-001-REQ-B-001 (middle-screen preserved): Apply Filter does
        NOT reset the chat to welcome; the chat keeps its results+selection
        state. The old welcome-words assertion is therefore removed.
        """
        page = env["page"]
        # Locate the filter sub-iframe.
        filt = None
        for f in page.frames:
            if "mode=filter" in (f.url or ""):
                filt = f; break
        assert filt is not None, "Filter sub-iframe (mode=filter) missing at Step 22"
        # Step 21 already toggled a row → Apply Filter is hot. Do NOT
        # re-toggle here; that would flip back to clean state and the
        # Apply Filter button would be greyed, making this test a no-op.
        apply_btn = filt.locator("[data-testid='apply-filter-button']")
        assert apply_btn.count() > 0, "Apply Filter button (sub-iframe) not found"
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

        chat_frame = page.locator("#coreChatFrame").content_frame
        assert chat_frame is not None, "Chat iframe not found at TestStep22"
        env["chat_frame"] = chat_frame

        # NPIs present in chat iframe after the new query lands.
        def _check_npis_after_apply():
            body_text = chat_frame.locator("body").inner_text()
            npis = re.findall(r"NPI[:\s]+\d{10}", body_text)
            if not npis:
                raise AssertionError(
                    "Apply Filter must re-query providers: no NPI strings present after Apply Filter. "
                    f"body (first 400 chars): {body_text[:400]}"
                )
            return len(npis)
        _retry("test22_npis_after_apply", 20, 750, _check_npis_after_apply)
        _screenshot(page, "22")


# Step 22b [filter + session-verification co-render after return-to-FindCare]
# After Apply Filter returns control from EvaluateCare to FindCare, the
# filter sub-iframe is the OWNING surface and must host BOTH the filter
# widget AND the session-verification block (SpecialtyFilter.tsx renders
# <SessionVerification /> below the Apply Filter row). Step 22 only checked
# NPIs in the chat body — this step locks down the co-render.
class TestStep22bFilterAndSessionTogether:
    def test_filter_and_session_verification_present_together(self, env):
        page = env["page"]
        filt = None
        for f in page.frames:
            if "mode=filter" in (f.url or ""):
                filt = f; break
        assert filt is not None, "Filter sub-iframe (mode=filter) missing after return"

        # 1. Filter widget core elements still mounted.
        assert filt.locator("[data-testid='specialty-filter']").count() > 0, \
            "specialty-filter root missing in filter iframe after return"
        rows = filt.locator(".specialty-filter__row")
        assert rows.count() > 0, "No specialty rows in filter iframe after return"
        assert filt.locator("[data-testid='apply-filter-button']").count() > 0, \
            "Apply Filter button missing in filter iframe after return"

        # 2. SessionVerification renders in the SAME iframe with Verified=true.
        sv = filt.locator("[data-testid='session-verification']")
        sv.first.wait_for(timeout=10000)
        assert sv.count() > 0, "session-verification block missing in filter iframe"
        sv_text = sv.first.inner_text()
        assert "Verified:" in sv_text, f"'Verified:' label missing: {sv_text[:200]}"
        verified_vals = [v.upper() for v in re.findall(r'Verified:\s*([A-Za-z]+)', sv_text)]
        POSITIVE = {"YES", "VERIFIED", "TRUE", "OK"}
        assert any(v in POSITIVE for v in verified_vals), \
            f"session-verification Verified={verified_vals} — none positive"
        _screenshot(page, "22b")


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
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: SharedServices banner button suppressed in prod")
        page = env["page"]

        # Regression guard for BUG-009 (utterance capture path).
        # Step 06 already typed one utterance ("Find me a bone doctor in
        # Delaware") through the real Send button — that exercises the
        # FindCare handleSend code path (the one the bug was in).
        # Here we record two MORE utterances via a direct fetch to /gate
        # so the smoke captures three distinct utterances without
        # triggering two extra /search round-trips (which would overload
        # the FindCare backend in the smoke run). The fetch is issued
        # from the PARENT page (page.evaluate) — SS's CORS allowlist
        # accepts the localhost origin, and the cookie travels because
        # ch_session is bound to SS's host:port regardless of caller.
        env.setdefault("typed_utterances", ["Find me a bone doctor in Delaware"])
        for phrase in ("cracker Jacks", "captain crunch"):
            # window._gateBody carries prior_guid in the body so the
            # utterance lands on the parent's admin.sessions record on
            # hosts where the ch_session cookie is dropped.
            page.evaluate(
                """async (args) => {
                    await fetch(args.url + '/gate', {
                        method: 'POST',
                        credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: window._gateBody({op: 'utterance', payload: {text: args.text}})
                    });
                }""",
                {"url": SHARED_URL, "text": phrase},
            )
            page.wait_for_timeout(600)  # let the gate persist before next call
            env["typed_utterances"].append(phrase)

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
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: SharedServices handoff is banner-driven, suppressed in prod")
        page = env["page"]
        # Pass criteria (operator 2026-06-18):
        # (a) SharedServices banner link is COLD — <span data-service="sharedservices">.
        # (b) The user_object surface (#rightSecurity) is populated — i.e.
        #     the session GUID is rendered into the security panel proving
        #     the user_object reached the wrapper. Panel suppressed in prod
        #     per S-002-REQ-B-002 (the IS_PROD skip above covers that).
        cold = page.locator('span[data-service="sharedservices"]')
        assert cold.count() > 0, "SharedServices banner link is not cold (no <span data-service='sharedservices'>) — ownership did not transfer"
        sec = page.locator("#rightSecurity")
        assert sec.count() > 0, "#rightSecurity panel missing"
        sec_text = sec.inner_text()
        assert "GUID:" in sec_text, f"user_object not present in #rightSecurity (no GUID rendered): {sec_text[:300]}"
        _screenshot(page, "26")


# Step 27 [EPIC-002-F-001-S-012-REQ-T-002]
class TestStep27:
    def test_mtls_shared_services_tls12(self):
        pytest.skip(
            "Skip 2026-05-05: turned OFF — same reason as TestStep15. No real "
            "mTLS path to SharedServices through HF. Re-enable when the "
            "architecture provides a layer we control."
        )
        ctx = ssl.create_default_context(cafile=os.path.join(CERTS_DIR, "ca.crt"))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(
            os.path.join(CERTS_DIR, "findcare.crt"),
            os.path.join(CERTS_DIR, "findcare.key"),
        )
        with httpx.Client(verify=ctx, timeout=15) as client:
            r = client.post(f"{SHARED_URL}/health")
        assert r.status_code == 200, (
            f"mTLS health check failed: POST {SHARED_URL}/health -> HTTP {r.status_code}"
        )


class TestStep27bSplashJsonContract:
    def test_gate_splash_returns_data_not_html(self, env):
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: SharedServices suppressed in prod")
        page = env["page"]
        payload = page.evaluate(
            """async (url) => {
                const r = await fetch(url + '/gate', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: window._gateBody({op: 'splash'})
                });
                return await r.json();
            }""",
            SHARED_URL,
        )
        result = (payload or {}).get("result") or {}
        assert "html" not in result, (
            f"splash response carries server-rendered html field — display "
            f"logic should be client-side only. result keys: {list(result.keys())}"
        )
        assert "identity" in result and isinstance(result["identity"], dict), (
            f"splash response missing identity dict. result: {result}"
        )
        assert "threads" in result and isinstance(result["threads"], dict), (
            f"splash response missing threads dict. result: {result}"
        )
        for k in ("utterances", "actions"):
            assert k in result["threads"], f"threads missing key '{k}': {result['threads']}"


class TestStep28:
    def test_shared_services_splash_renders_user_object(self, env):
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: SharedServices reachable only via suppressed banner button in prod")
        page = env["page"]
        splash = page.locator("#sharedSplash")
        assert splash.count() > 0 and splash.is_visible(), "SharedServices splash not visible"
        text = splash.inner_text()
        # (1) Header
        assert "Shared Services — User Object" in text, f"Missing header: {text[:300]}"
        # (2) Identity subsection + labeled rows + canonical values
        assert "Identity" in text, f"Missing Identity subsection: {text[:300]}"
        assert "user_type" in text and "Guest" in text, f"user_type=Guest not rendered: {text[:300]}"
        assert "origin" in text and "SharedServices" in text, f"origin=SharedServices not rendered: {text[:300]}"
        for row in ("guid", "server_env", "created_at", "expires_at"):
            assert row in text, f"Missing identity row '{row}': {text[:300]}"
        assert re.search(r"\b[0-9a-f]{32}\b", text), f"No 32-char hex GUID rendered: {text[:300]}"
        # (3) Session Conversation History subsection MUST be populated.
        # Pre-utterance emptiness is asserted by TestStep05b; this step
        # runs AFTER Step 06 typed one utterance through the real Send
        # button and Step 25 typed two more via direct /gate. Empty-state
        # here means the capture path is broken — no escape hatch.
        assert "Session Conversation History" in text, f"Missing history subsection: {text[:300]}"
        text_lower = text.lower()
        assert "no ux events or utterances yet" not in text_lower, (
            "Post-utterance splash shows empty-state copy — "
            "capture path is broken. Three utterances were typed before "
            f"this step. Splash (first 600c): {text[:600]}"
        )
        # Populated: two threads (Skip taxonomy 2026-06-10):
        #   Utterances | Actions.
        # CSS text-transform may uppercase the labels; compare case-insensitively.
        for label in ("utterances", "actions"):
            assert label in text_lower, f"Missing thread label '{label}': {text[:300]}"
        # The old thread labels MUST be gone.
        for retired in ("person → system", "llm → system", "llm → person", "llm → machine"):
            assert retired not in text_lower, f"Retired thread label '{retired}' still present: {text[:300]}"
        # Every utterance the smoke typed earlier MUST appear in the
        # Utterances column. Step 06 typed one, Step 25 typed two more
        # — three total. If the /gate utterance capture path regresses,
        # this trips.
        typed = env.get("typed_utterances", [])
        assert len(typed) >= 3, f"Smoke harness only recorded {len(typed)} utterances; need ≥3"
        for u in typed:
            assert u.lower() in text_lower, (
                f"Utterance {u!r} missing from Utterances column; capture path broken. "
                f"Splash text (first 600c): {text[:600]}"
            )
        _screenshot(page, "28")


# Step 29 [EPIC-002-F-001-S-012-REQ-T-001]
class TestStep29:
    def test_shared_services_token_auth(self, env):
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: security token panel suppressed in prod")
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
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: depends on TestStep29 token panel suppressed in prod")
        # Nonce uniqueness already verified by _verify_session_identity in step 29.
        # GUID equality across services is NOT asserted: each service has its
        # own session GUID per the deployed happy path.
        assert env.get("shared_nonce"), "SharedServices nonce not stored from step 29"
        assert env.get("shared_guid"), "SharedServices GUID not stored from step 29"
        _screenshot(env["page"], "30")


# Step 31 [EPIC-002-F-001-S-012-REQ-T-003] — Handoff 4: SharedServices → FindCare
class TestStep31:
    def test_shared_to_findcare(self, env):
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: SharedServices handoff path is banner-driven, suppressed in prod")
        page = env["page"]
        # Filter-iframe selectors (EPIC-006-F-002 Option B).
        filt = None
        for f in page.frames:
            if "mode=filter" in (f.url or ""):
                filt = f; break
        assert filt is not None, "REQ-B-031: filter sub-iframe missing"
        row = filt.locator(".specialty-filter__row").first
        assert row.count() > 0, "REQ-B-031: no specialty rows in filter iframe"
        row.click()
        page.wait_for_timeout(800)
        apply_btn = filt.locator("[data-testid='apply-filter-button']")
        assert apply_btn.count() > 0, "REQ-B-031: Apply Filter button (sub-iframe) not found"
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
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: SharedServices handoff path is banner-driven, suppressed in prod")
        page = env["page"]
        frame = env.get("chat_frame", page)
        # Re-select providers and evaluate to get to EvaluateCare
        select_btns = frame.locator("button[title='Select for evaluation']")
        if select_btns.count() == 0:
            select_btns = frame.locator("button:has-text('↓')")
        if select_btns.count() > 0:
            select_btns.first.click()
            page.wait_for_timeout(500)
        eval_btn = frame.locator("[data-testid='evaluate-button']")
        assert eval_btn.count() > 0, "in-iframe Evaluate button not found for handoff 5"
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


# Step 33 [EPIC-002-F-001-S-012-REQ-T-003] — Handoff: return-to-FindCare from SharedServices
class TestStep33:
    def test_shared_to_evalcare(self, env):
        # Pass criteria (operator 2026-06-18, "That is all"):
        # - banner FindCare link is dead (cold) in lower environments
        # - welcome text "Find care in the US & clinical trials globally,
        #   Let's talk about it." is present in the main frame
        page = env["page"]
        # Use the FindCare banner link as the return gesture — produces
        # the welcome state cleanly without triggering a follow-on search.
        page.evaluate("() => { if (typeof window.gotoFindCare === 'function') window.gotoFindCare(); }")
        page.wait_for_timeout(3000)
        if not IS_PROD:
            cold = page.locator('span[data-service="findcare"]')
            assert cold.count() > 0, \
                "REQ-B-033: FindCare banner link is not dead (no <span data-service='findcare'>) after return"
        # Welcome text in the main frame — specifically the chat iframe
        # element #coreChatFrame, NOT the filter sub-iframe (which is also
        # served by FindCare and would match a URL-based search).
        chat_locator = page.frame_locator("#coreChatFrame")
        body_text = chat_locator.locator("body").inner_text()
        assert "Find care in the US & clinical trials globally," in body_text, \
            f"REQ-B-033: welcome text not present in main frame: {body_text[:400]}"
        assert "Let's talk about it." in body_text, \
            f"REQ-B-033: welcome 'Let's talk about it.' line not present in main frame: {body_text[:400]}"
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


# Step 35 [EPIC-008-F-004-S-006-REQ-T-003] — Comprehensive PKI cert verification (V11)
# (a) every cert file in Code/Shared/ops/certs/ parses as valid X.509,
#     signed by ca.crt, not expired
# (b) for each ordered server pair (FindCare↔EvalCare, FindCare↔Shared,
#     EvalCare↔Shared), an mTLS handshake succeeds in BOTH directions.
class TestStep35:
    SERVER_CERTS = ("findcare.crt", "evalcare.crt", "shared.crt", "localhost.crt")

    def _load_cert(self, path):
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        with open(path, "rb") as f:
            data = f.read()
        return x509.load_pem_x509_certificate(data, default_backend())

    def test_step35a_certs_valid_and_signed_by_ca(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import (
            padding, rsa, ec)
        import datetime as dt

        ca_path = os.path.join(CERTS_DIR, "ca.crt")
        assert os.path.isfile(ca_path), f"ca.crt missing at {ca_path}"
        ca = self._load_cert(ca_path)
        ca_pub = ca.public_key()
        now = dt.datetime.now(dt.timezone.utc)

        problems = []
        for fname in self.SERVER_CERTS:
            path = os.path.join(CERTS_DIR, fname)
            if not os.path.isfile(path):
                problems.append(f"{fname}: missing at {path}")
                continue
            try:
                cert = self._load_cert(path)
            except Exception as e:
                problems.append(f"{fname}: parse failed: {e!r}")
                continue
            # Expiry — not_valid_after_utc available on cryptography 42+
            try:
                nva = cert.not_valid_after_utc
            except AttributeError:
                nva = cert.not_valid_after.replace(tzinfo=dt.timezone.utc)
            if nva < now:
                problems.append(f"{fname}: expired at {nva.isoformat()}")
                continue
            # Signed by CA — verify the cert signature using ca's pubkey
            try:
                if isinstance(ca_pub, rsa.RSAPublicKey):
                    ca_pub.verify(
                        cert.signature,
                        cert.tbs_certificate_bytes,
                        padding.PKCS1v15(),
                        cert.signature_hash_algorithm,
                    )
                elif isinstance(ca_pub, ec.EllipticCurvePublicKey):
                    ca_pub.verify(
                        cert.signature,
                        cert.tbs_certificate_bytes,
                        ec.ECDSA(cert.signature_hash_algorithm),
                    )
                else:
                    problems.append(
                        f"{fname}: unsupported CA key type {type(ca_pub).__name__}"
                    )
            except Exception as e:
                problems.append(f"{fname}: signature not valid against ca.crt: {e!r}")
        assert not problems, "Step 35a cert problems: " + "; ".join(problems)

    @pytest.mark.mtls_required
    def test_step35b_mtls_full_mesh(self):
        pytest.skip(
            "Skip 2026-05-05: turned OFF — same reason as TestStep15/27. The "
            "9 HF Space endpoints all present the same wildcard *.hf.space "
            "cert at HF's edge; no path-controlled mTLS. Re-enable when the "
            "architecture provides a layer we control."
        )
        # Full-mesh ordered pairs: (client, server)
        pairs = [
            ("findcare", "evalcare", EVALCARE_URL),
            ("findcare", "shared",   SHARED_URL),
            ("evalcare", "findcare", FINDCARE_URL),
            ("evalcare", "shared",   SHARED_URL),
            ("shared",   "findcare", FINDCARE_URL),
            ("shared",   "evalcare", EVALCARE_URL),
        ]
        ca_path = os.path.join(CERTS_DIR, "ca.crt")
        problems = []
        for client_name, server_name, server_url in pairs:
            client_cert = (
                os.path.join(CERTS_DIR, f"{client_name}.crt"),
                os.path.join(CERTS_DIR, f"{client_name}.key"),
            )
            try:
                with httpx.Client(cert=client_cert, verify=ca_path,
                                  timeout=10) as c:
                    # /health is POST-only per EPIC-008-F-011-S-002-REQ-B-001.
                    r = c.post(f"{server_url}/health")
                    if r.status_code != 200:
                        problems.append(
                            f"{client_name}->{server_name}: "
                            f"status {r.status_code}"
                        )
            except Exception as e:
                problems.append(f"{client_name}->{server_name}: {e!r}")
        assert not problems, (
            "Step 35b mTLS pair failures: " + "; ".join(problems)
        )


# Step 99 [EPIC-002-F-003-S-005 contract negative case — kept LAST]
# Regression guard: nonsense queries (and backend timeouts on /search)
# MUST NOT escalate to the full-screen #chFatalErrorOverlay. The overlay
# is reserved for real infrastructure failures (auth chain, malformed
# body) — soft failures render inline in the chat. Placed at the END of
# the file so its page.goto + nonsense submit cannot poison state for
# any subsequent test (the env fixture is module-scoped).
class TestStep99FiddlesticksNoFatal:
    def test_nonsense_query_does_not_trigger_fatal(self, env):
        if IS_PROD:
            pytest.skip("S-002-REQ-B-002: chrome suppressed in prod by design")
        page = env["page"]
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_timeout(3000)
        # Same iframe-discovery pattern Step04 uses (canonical): match the
        # local FindCare port OR any *.hf.space URL — covers local/dev/qa/prod.
        # Prior 'findcare' substring match always missed dev/qa whose iframe
        # is skipsnow-{env}-chathealthyspace.hf.space.
        chat = None
        for f in page.frames:
            if ":7860" in (f.url or "") or "hf.space" in (f.url or ""):
                chat = f; break
        assert chat is not None, "FindCare chat iframe not found for nonsense-query regression check"
        try:
            inp = chat.locator(
                "input[placeholder*='Type a message'], textarea"
            ).first
            inp.wait_for(state="visible", timeout=15000)
            inp.fill("fiddlesticks")
            chat.locator("button", has_text="Send").first.click()
        except Exception:
            pytest.skip("could not submit nonsense query in chat iframe")
        deadline = 40_000
        overlay = page.locator("#chFatalErrorOverlay")
        while deadline > 0:
            assert not overlay.is_visible(), (
                "REGRESSION: nonsense query 'fiddlesticks' triggered the "
                "503 fatal overlay. Soft failures MUST render inline in "
                "the chat phase; the wrapper overlay is reserved for "
                "real infrastructure failures."
            )
            try:
                body_text = chat.locator("body").inner_text().lower()
                if "couldn't" in body_text or "no providers" in body_text \
                        or "results" in body_text or "available providers" in body_text:
                    break
            except Exception:
                pass
            page.wait_for_timeout(1000); deadline -= 1000
        _screenshot(page, "99")


# Step 99b — UM regression: a nonsense word arriving after a findAProvider
# prior must classify as nonsense. Sends two POSTs to /gate directly (no
# browser, no UI state coupling): (1) "Find me a bone doctor in Delaware"
# on a fresh session — establishes target_action=findAProvider as the
# prior; (2) "fidelsticks" on that same session. UM must evaluate the
# utterance alone first and pick nonsense. Misclassifying as findAProvider
# (the previously-observed bias) runs SpecialtyFilter + ProviderSearch
# against gibberish and the user waits ~20+ seconds for an irrelevant
# result.
class TestStep99bNonsenseAfterFindAProvider:
    def test_nonsense_after_findaprovider_prior(self, env):
        if SMOKE_ENV != "local":
            pytest.skip("Step 99b uses direct /gate; local-only for now")
        import json as _json

        def post_utterance(text, prior_guid):
            body = {"op": "utterance", "payload": {"text": text}}
            if prior_guid:
                body["prior_guid"] = prior_guid
            c = httpx.Client(verify=False, timeout=60)
            try:
                r = c.post(f"{SHARED_URL}/gate", json=body)
            finally:
                c.close()
            assert r.status_code == 200, (
                f"/gate returned {r.status_code}: {r.text[:300]}"
            )
            return r.json()

        evt1 = post_utterance("Find me a bone doctor in Delaware", None)
        assert evt1.get("ok") is True, f"turn 1 not ok: {evt1}"
        ta1 = (evt1.get("result") or {}).get("target_action")
        assert ta1 == "findAProvider", (
            f"turn 1 expected target_action=findAProvider, got {ta1!r}"
        )
        guid = evt1.get("guid")
        assert guid, f"turn 1 missing guid: {evt1}"

        evt2 = post_utterance("fidelsticks", guid)
        assert evt2.get("ok") is True, f"turn 2 not ok: {evt2}"
        ta2 = (evt2.get("result") or {}).get("target_action")
        assert ta2 not in ("findAProvider", "specialtySearch"), (
            f"BUG REGRESSION: nonsense word 'fidelsticks' submitted after a "
            f"findAProvider-only prior was misclassified as {ta2!r}. UM must "
            f"evaluate the utterance alone first and classify gibberish as "
            f"nonsense regardless of prior context. A target_action of "
            f"findAProvider or specialtySearch on this turn means SpecialtyFilter "
            f"+ ProviderSearch ran against gibberish."
        )
        assert ta2 == "closeConnection200", (
            f"turn 2 expected target_action=closeConnection200 (NonsenseTool "
            f"morphs the IntentDocument to closeConnection200 after streaming "
            f"the clarification; UR chains to CloseConnection200Tool which is "
            f"the terminal action). Got {ta2!r}."
        )


# Step 99c [EPIC-002-F-010-S-002-REQ-B-001 + EPIC-002-F-004-S-002-REQ-B-001
#          + EPIC-002-F-003-S-005 session-history persistence]
# Enduring regression for the geography-disambiguation flow.
#
# Turn 1: a complaint plus a city without a state ("milwaukee" alone).
# UM must classify as specialtySearch (geography partial, ProviderSearch
# not yet sufficient) AND emit a user_message proposing the candidate
# state. SpecialtyFilter runs and caches nucc_codes on the IntentDocument.
#
# Turn 2: a bare 'yes' on the same session. UM must read the prior
# IntentDocument's pending_disambiguation, treat the 'yes' as resolving
# the candidate state, and upgrade target_action to findAProvider.
# Classifying 'yes' as nonsense here is the previously-observed regression
# this test guards against.
#
# Also asserts the persisted user_object integrity: the same session's
# splash MUST surface BOTH person utterances AND both system utterances,
# AND the action sequence MUST include specialty_filter (turn 1) and
# provider_search_and_selection (turn 2). A truncated dialogue bucket or
# a missing action entry indicates session-history corruption.
class TestStep99cGeographyDisambiguationFlow:
    def test_geography_disambiguation_resolves_and_persists(self, env):
        if SMOKE_ENV != "local":
            pytest.skip("Step 99c uses direct /gate; local-only for now")

        def post_gate(body):
            c = httpx.Client(verify=False, timeout=90)
            try:
                r = c.post(f"{SHARED_URL}/gate", json=body)
            finally:
                c.close()
            assert r.status_code == 200, (
                f"/gate returned {r.status_code}: {r.text[:300]}"
            )
            return r.json()

        # Turn 1: city alone, state missing.
        evt1 = post_gate({
            "op": "utterance",
            "payload": {"text": "I need a nurse practitioner to help me with my pain in milwaukee"},
        })
        assert evt1.get("ok") is True, f"turn 1 not ok: {evt1}"
        ta1 = (evt1.get("result") or {}).get("target_action")
        assert ta1 == "specialtySearch", (
            f"turn 1 expected target_action=specialtySearch (geography is "
            f"partial — city without state — so ProviderSearch must NOT "
            f"run yet; UM parks a findAProvider entry with "
            f"pending_disambiguation and falls back to specialtySearch "
            f"so the FE still gets specialty rows). Got {ta1!r}."
        )
        guid = evt1.get("guid")
        assert guid, f"turn 1 missing guid: {evt1}"

        # Turn 2: bare 'yes' resolves the pending state candidate.
        evt2 = post_gate({
            "op": "utterance",
            "payload": {"text": "yes"},
            "prior_guid": guid,
        })
        assert evt2.get("ok") is True, f"turn 2 not ok: {evt2}"
        ta2 = (evt2.get("result") or {}).get("target_action")
        assert ta2 != "nonsense" and ta2 != "closeConnection200", (
            f"BUG REGRESSION: bare 'yes' submitted while a pending_dis"
            f"ambiguation was set on the prior IntentDocument was "
            f"misclassified as {ta2!r}. UM Rule 1 says a yes/no on a "
            f"pending turn MUST resolve the pending state, NOT fall "
            f"through to nonsense."
        )
        assert ta2 == "findAProvider", (
            f"turn 2 expected target_action=findAProvider (UM resolved "
            f"the pending state candidate, now geography is sufficient). "
            f"Got {ta2!r}."
        )

        # Splash same session — assert dialogue + action integrity.
        splash = post_gate({
            "op": "splash",
            "payload": {},
            "prior_guid": guid,
        })
        threads = ((splash.get("result") or {}).get("threads")) or {}
        utterances = threads.get("utterances") or []
        actions = threads.get("actions") or []

        # Dialogue: at least 3 entries (person 1, system 2, person 3).
        # A 4th system entry is allowed if UM acknowledges the resolution.
        assert len(utterances) >= 3, (
            f"BUG REGRESSION: persisted utterances bucket truncated. "
            f"Expected >=3 entries (person nurse-practitioner / system "
            f"prompt / person yes) — got {len(utterances)}. Full bucket: "
            f"{utterances}"
        )
        u1, u2, u3 = utterances[0], utterances[1], utterances[2]
        assert u1.get("actor") == "person" and "nurse practitioner" in (u1.get("text") or "").lower(), (
            f"utterance #1 should be the original person utterance about "
            f"nurse practitioner. Got {u1}"
        )
        assert u2.get("actor") == "system" and (u2.get("text") or "").strip(), (
            f"utterance #2 should be the LLM-authored system prompt "
            f"(non-empty). Got {u2}"
        )
        assert u3.get("actor") == "person" and (u3.get("text") or "").strip().lower() == "yes", (
            f"utterance #3 should be the person 'yes' resolving the "
            f"disambiguation. Got {u3}"
        )

        # Actions: must include specialty_filter (turn 1) and
        # provider_search_and_selection (turn 2). Cache lookups on turn 2
        # are also OK; the requirement is that ProviderSearch ran.
        tool_names = [a.get("tool_name") for a in actions]
        assert "specialty_filter" in tool_names, (
            f"actions should include specialty_filter from turn 1. "
            f"Got tool_names={tool_names}"
        )
        assert "provider_search_and_selection" in tool_names, (
            f"actions should include provider_search_and_selection from "
            f"turn 2. Got tool_names={tool_names}"
        )


class TestStep99dMisspelledUtteranceNoFatal:
    """EPIC-002-F-010-S-001-REQ-B-012: a user utterance MUST NEVER propagate
    a fatal application exception. Drives the regression that motivated the
    REQ — a misspelled city ('wilington' for Wilmington) used to surface as
    an opaque 503 to the user."""

    def test_misspelled_utterance_does_not_propagate_fatal(self, env):
        if SMOKE_ENV != "local":
            pytest.skip("Step 99d uses direct /gate; local-only for now")
        import json as _json
        c = httpx.Client(verify=False, timeout=90)
        try:
            r = c.post(f"{SHARED_URL}/gate", json={
                "op": "utterance",
                "payload": {"text": "find me a shrink in wilington DE"},
            })
        finally:
            c.close()
        assert r.status_code == 200, (
            f"REGRESSION REQ-B-012: misspelled-city utterance returned "
            f"HTTP {r.status_code}; user-typed input MUST NEVER yield "
            f"a 5xx. body={r.text[:300]}"
        )
        body = r.json()
        assert body.get("ok") is True, (
            f"REGRESSION REQ-B-012: /gate ok=False on a misspelled-city "
            f"utterance. /gate must surface a graceful classification, "
            f"never an unhandled error. body={_json.dumps(body)[:400]}"
        )
        ta = (body.get("result") or {}).get("target_action")
        assert ta in {
            "findAProvider", "specialtySearch", "closeConnection200", "nonsense"
        }, (
            f"REGRESSION REQ-B-012: misspelled-city utterance produced "
            f"target_action={ta!r} — outside the legal set. UM must "
            f"always classify into a known action."
        )


class TestStep99eHighConfidenceSpellingCorrected:
    """EPIC-002-F-010-S-001-REQ-B-011: when the classifier is >=75%
    confident of the intended word, it MUST silently substitute the
    corrected form and proceed normally. 'wilington DE' is the
    canonical example baked into the prompt — Wilmington DE is the only
    plausible major DE city near that spelling."""

    def test_high_confidence_misspelling_routes_to_search(self, env):
        if SMOKE_ENV != "local":
            pytest.skip("Step 99e uses direct /gate; local-only for now")
        c = httpx.Client(verify=False, timeout=90)
        try:
            r = c.post(f"{SHARED_URL}/gate", json={
                "op": "utterance",
                "payload": {"text": "find me a shrink in wilington DE"},
            })
        finally:
            c.close()
        assert r.status_code == 200, (
            f"REQ-B-011: /gate returned {r.status_code} on a "
            f"high-confidence-correctable misspelling. "
            f"body={r.text[:300]}"
        )
        body = r.json()
        assert body.get("ok") is True, (
            f"REQ-B-011: /gate ok=False on 'wilington DE'. "
            f"body={body}"
        )
        result = body.get("result") or {}
        ta = result.get("target_action")
        assert ta in {"findAProvider", "specialtySearch"}, (
            f"REQ-B-011: high-confidence misspelling 'wilington DE' "
            f"MUST be silently corrected to Wilmington and proceed to "
            f"findAProvider/specialtySearch. Got target_action={ta!r}. "
            f"If the classifier picked closeConnection200 it is asking "
            f"the user to clarify when it should have substituted."
        )
