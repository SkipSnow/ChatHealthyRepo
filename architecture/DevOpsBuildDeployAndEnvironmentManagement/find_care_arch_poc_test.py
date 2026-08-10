"""Architecture-compliance POC test. Validates the shell + each ported
capability against the contract in
brain/BusinessArtifacts/architecture/FrontEnd/FrontEndArchitecture.pptx.

Order is gated with -x: each test guards the next. Add a new test class
here as each new capability is ported through ClientRouter."""

import os
import pytest
from playwright.sync_api import sync_playwright, expect

import sys as _sys, pathlib as _pl


for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "FrontEndApplicationLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()


_DIGITS = frozenset("0123456789")
_LABEL_SEPARATORS = frozenset(': \t\r\n')


def _labelled_digit_runs(text, label, length):
    """Digit runs of `length` that follow `label` and any separators."""
    out = []
    at = text.find(label)
    while at != -1:
        k = at + len(label)
        while k < len(text) and text[k] in _LABEL_SEPARATORS:
            k += 1
        j = k
        while j < len(text) and text[j] in _DIGITS:
            j += 1
        if j - k == length:
            out.append(text[k:j])
        at = text.find(label, at + 1)
    return out


def _nct_ids(text):
    """Clinical-trial identifiers: NCT followed by digits."""
    out = []
    at = text.find("NCT")
    while at != -1:
        j = at + 3
        while j < len(text) and text[j].isdigit():
            j += 1
        if j > at + 3:
            out.append(text[at:j])
        at = text.find("NCT", at + 1)
    return out


BASE_URL = os.environ.get("SMOKE_TEST_BASE_URL", "https://localhost/")


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900},
            ignore_https_errors=True,
        )
        pg = ctx.new_page()
        pg.on("console", lambda m: _CH_LOG.info(f"[CONSOLE {m.type}] {m.text}"))
        pg.on("pageerror", lambda e: _CH_LOG.info(f"[PAGE ERROR] {e}"))
        yield pg
        ctx.close()
        browser.close()


class TestStep01FrameSet:
    """The seven named frames per Slide 2 exist as DOM containers."""

    def test_seven_frames_present(self, page):
        page.goto(BASE_URL, wait_until="domcontentloaded")
        for name in (
            "Header", "Footer", "LeftPanel", "RightPanel",
            "MainWindow", "UserMessage", "UserPromptAndControl",
        ):
            expect(page.locator(f"#frame_{name}")).to_have_count(1, timeout=10_000)


class TestStep02ClientRouterLoaded:
    """ClientRouter is the single client-side router per Slide 1."""

    def test_client_router_global(self, page):
        page.wait_for_function(
            "() => window.ClientRouter && typeof window.ClientRouter.render === 'function' "
            "&& typeof window.ClientRouter.getStreamedPayloads === 'function'",
            timeout=10_000,
        )


class TestStep03FooterPortedViaRouter:
    """FooterWidget renders the Footer chrome via router:render. Proves
    React → ClientRouter → frame_Footer is wired end-to-end."""

    def test_footer_populated_by_widget(self, page):
        page.wait_for_function(
            "() => document.querySelector('#frame_Footer a[data-router-action=\"about_chathealthy\"]') != null",
            timeout=30_000,
        )
        link = page.locator('#frame_Footer a[data-router-action="about_chathealthy"]')
        expect(link).to_have_text("About ChatHealthy.ai")


class TestStep04AboutPopupEndToEnd:
    """Click About → router:action → router:makeCall → SS tool streams
    kind:'about_chathealthy' → AboutChatHealthyWidget builds HTML →
    router:render(popup:true) → centered popup shows About content."""

    def test_about_link_click_opens_popup(self, page):
        link = page.locator('#frame_Footer a[data-router-action="about_chathealthy"]')
        link.click()
        page.wait_for_selector("#popup_AboutChatHealtyPopUP", timeout=30_000)
        popup = page.locator("#popup_AboutChatHealtyPopUP")
        expect(popup).to_be_visible()
        expect(popup).to_contain_text("About ChatHealthy.ai")
        expect(popup).to_contain_text("Build")


class TestStep05HeaderPortedViaRouter:
    """HeaderWidget renders Header frame chrome via router:render."""

    def test_header_populated_by_widget(self, page):
        page.wait_for_function(
            "() => document.querySelector('#frame_Header button[data-router-action=\"oauth_start\"]') != null",
            timeout=30_000,
        )
        login = page.locator('#frame_Header button[data-router-action="oauth_start"]')
        expect(login).to_have_text("Login & Registration")
        expect(page.locator('#frame_Header')).to_contain_text("Pre-Alpha")
        expect(page.locator('#frame_Header')).to_contain_text("ChatHealthy")


class TestStep06WelcomePainted:
    """WelcomeWidget paints the welcome message into frame_MainWindow."""

    def test_welcome_in_main_window(self, page):
        page.wait_for_function(
            "() => (document.querySelector('#frame_MainWindow')?.innerText || '').includes('Find care')",
            timeout=30_000,
        )
        expect(page.locator('#frame_MainWindow')).to_contain_text("Find care in the US")
        expect(page.locator('#frame_MainWindow')).to_contain_text("Let's talk about it")


class TestStep07UserPromptPortedViaRouter:
    """UserPromptWidget paints the input form into frame_UserPromptAndControl."""

    def test_prompt_form_present(self, page):
        page.wait_for_function(
            "() => document.querySelector('#frame_UserPromptAndControl form[data-router-action=\"user:submit\"]') != null",
            timeout=30_000,
        )
        form = page.locator('#frame_UserPromptAndControl form[data-router-action="user:submit"]')
        expect(form).to_be_visible()
        expect(form.locator('input[name="text"]')).to_be_visible()
        expect(form.locator('button[type="submit"]')).to_have_text("Send")


class TestStep08SearchingIndicatorOnSubmit:
    """Submit a query → ProviderResultsWidget paints "Searching for:" into
    frame_MainWindow before any backend stream arrives."""

    def test_searching_indicator(self, page):
        form = page.locator('#frame_UserPromptAndControl form[data-router-action="user:submit"]')
        form.locator('input[name="text"]').fill("Find me a bone doctor in Delaware")
        form.locator('button[type="submit"]').click()
        page.wait_for_function(
            "() => (document.querySelector('#frame_MainWindow')?.innerText || '').includes('Searching for')",
            timeout=10_000,
        )
        expect(page.locator('#frame_MainWindow')).to_contain_text("bone doctor in Delaware")


class TestStep09ProvidersStreamIntoMainWindow:
    """End-to-end: utterance → SS gate → tools → providers broadcast →
    ProviderResultsWidget paints NPI rows into frame_MainWindow."""

    def test_provider_rows_arrive(self, page):
        page.wait_for_function(
            "() => /NPI:\\s*\\d{10}/.test(document.querySelector('#frame_MainWindow')?.innerText || '')",
            timeout=60_000,
        )
        text = page.locator('#frame_MainWindow').inner_text()
        assert _labelled_digit_runs(text, "NPI", 10), \
            f"No NPI numbers in MainWindow: {text[:300]}"


class TestStep10ProviderDetailIntoRightPanel:
    """Click a provider name → router:action 'provider:detail' →
    router:makeCall(op:'provider-detail') → SS routes to FindCare →
    kind:'provider-detail' broadcast → ProviderDetailWidget paints into
    frame_RightPanel."""

    def test_click_provider_paints_right_panel(self, page):
        link = page.locator('#frame_MainWindow a[data-router-action="provider:detail"]').first
        expect(link).to_be_visible()
        link.click()
        page.wait_for_function(
            "() => /practice addresses/i.test(document.querySelector('#frame_RightPanel')?.innerText || '')",
            timeout=60_000,
        )
        expect(page.locator('#frame_RightPanel')).to_contain_text("Practice addresses")
        expect(page.locator('#frame_RightPanel')).to_contain_text("NPI:")


class TestStep11LegalPanelOpensPopup:
    """Click a footer panel link → router:action 'open_panel' →
    LegalPanelWidget renders popup with an iframe pointing at the static
    page."""

    def test_roadmap_link_opens_popup(self, page):
        link = page.locator('#frame_Footer a[data-router-action="open_panel"][data-path="roadmap.html"]')
        expect(link).to_be_visible()
        link.click()
        popup = page.locator('#popup_LegalPanel_roadmap_html')
        expect(popup).to_be_visible(timeout=10_000)
        expect(popup.locator('iframe[src="/roadmap.html"]')).to_have_count(1)


class TestStep12OAuthLoginPresent:
    """Login & Registration button exists with data-router-action='oauth_start'
    so OAuthLoginWidget can wire the click → SS /auth/google/login redirect."""

    def test_login_button_present(self, page):
        btn = page.locator('#frame_Header button[data-router-action="oauth_start"]')
        expect(btn).to_be_visible()
        expect(btn).to_have_text("Login & Registration")


class TestStep13SpecialtyFilterPaintsLeftPanel:
    """After a search, the SpecialtyFilterWidget receives kind:'specialties'
    broadcast and paints the header ('Choose Specialties' title, count cells,
    Check All / Prescribers / Homeopathic controls, scrollable rows, and the
    Apply Filter button) into frame_LeftPanel."""

    def test_left_panel_has_filter(self, page):
        page.wait_for_function(
            "() => (document.querySelector('#frame_LeftPanel')?.innerText || '').includes('Choose Specialties')",
            timeout=30_000,
        )
        expect(page.locator('#frame_LeftPanel [data-testid="apply-filter-button"]')).to_be_visible()
        expect(page.locator('#frame_LeftPanel [data-testid="count-all-possible"]')).to_be_visible()
        expect(page.locator('#frame_LeftPanel [data-testid="count-all-prescribers"]')).to_be_visible()
        expect(page.locator('#frame_LeftPanel [data-testid="count-your-choices"]')).to_be_visible()
        expect(page.locator('#frame_LeftPanel [data-testid="macro-prescribers"]')).to_be_visible()
        expect(page.locator('#frame_LeftPanel [data-testid="macro-homeopathic"]')).to_be_visible()


class TestStep14SharedServicesSplash:
    """ClientRouter.getStreamedPayloads(op:'splash') → SS streams kind:'splash' →
    SharedServicesSplashWidget paints user_object into frame_MainWindow."""

    def test_splash_paints_main_window(self, page):
        page.evaluate("""() => {
            window.ClientRouter.getStreamedPayloads({op:'splash', payload:{}});
        }""")
        page.wait_for_function(
            "() => (document.querySelector('#frame_MainWindow')?.innerText || '').includes('SharedServices')",
            timeout=30_000,
        )
        expect(page.locator('#frame_MainWindow')).to_contain_text("User Object")


class TestStep15EvaluateCareSplash:
    """ClientRouter.getStreamedPayloads(op:'evalcare-splash') → SS hops to EvaluateCare →
    EvaluateCareSplashWidget paints into frame_MainWindow."""

    def test_evalcare_splash_paints_main_window(self, page):
        page.evaluate("""() => {
            window.ClientRouter.getStreamedPayloads({op:'evalcare-splash', payload:{}});
        }""")
        page.wait_for_function(
            "() => (document.querySelector('#frame_MainWindow')?.innerText || '').length > 30",
            timeout=30_000,
        )


class TestStep15bSystemMessagePromptPaints:
    """When SS UR streams kind:'prompt' (e.g., UM clarification), the
    SystemMessageWidget paints the text into frame_UserMessage so the
    user reads the system response alongside the running search context."""

    def test_prompt_paints_user_message(self, page):
        page.goto("https://localhost/", wait_until="networkidle")
        page.wait_for_function(
            "() => document.querySelector('#frame_UserPromptAndControl form') != null",
            timeout=15_000,
        )
        page.locator("#frame_UserPromptAndControl input[name='text']").fill(
            "find clinical trials for type 2 diabetes; subject age 55"
        )
        page.locator("#frame_UserPromptAndControl button[type='submit']").click()
        page.wait_for_function(
            "() => (document.querySelector('#frame_UserMessage')?.innerText || '').length > 20",
            timeout=60_000,
        )
        msg = page.locator('#frame_UserMessage').inner_text()
        assert "type 2 diabetes" in msg.lower(), f"Prompt not painted: {msg[:200]}"


class TestStep15cMobileNavDrawer:
    """Set narrow viewport → hamburger button visible → click → drawer
    popup contains the same nav buttons as the desktop header."""

    def test_mobile_drawer_opens(self, page):
        page.set_viewport_size({"width": 480, "height": 800})
        page.goto("https://localhost/", wait_until="networkidle")
        page.wait_for_function(
            "() => document.querySelector('#frame_Header button.ch-header-hamburger') != null",
            timeout=15_000,
        )
        hamburger = page.locator('#frame_Header button.ch-header-hamburger')
        expect(hamburger).to_be_visible()
        hamburger.click()
        popup = page.locator('#popup_MobileNavDrawer')
        expect(popup).to_be_visible(timeout=10_000)
        expect(popup.locator('button[data-router-action="oauth_start"]')).to_have_text("Login & Registration")
        # Restore for subsequent tests in this module.
        page.set_viewport_size({"width": 1400, "height": 900})


class TestStep15dSpecialtyFilterApply:
    """Click Apply Filter → SpecialtyFilterWidget fires
    router:makeCall(op:'apply_filter'). Apply is dirty-gated per prod, so
    we toggle one row first to flip the dirty state and enable the button."""

    def test_apply_filter_call_succeeds(self, page):
        page.goto("https://localhost/", wait_until="networkidle")
        page.wait_for_function(
            "() => document.querySelector('#frame_UserPromptAndControl form') != null",
            timeout=15_000,
        )
        page.locator("#frame_UserPromptAndControl input[name='text']").fill("Find me a bone doctor in Delaware")
        page.locator("#frame_UserPromptAndControl button[type='submit']").click()
        page.wait_for_function(
            "() => document.querySelector('#frame_LeftPanel button[data-router-action=\"filter:apply\"]') != null",
            timeout=60_000,
        )
        page.evaluate("""() => {
            window.__applyFilterCalled = false;
            window.addEventListener('message', function(ev){
                var msg = ev.data;
                if (msg && msg.type === 'router:makeCall' && msg.op === 'apply_filter') {
                    window.__applyFilterCalled = true;
                }
            });
        }""")
        # Toggle one specialty row to flip the dirty state → Apply enables.
        page.locator('#frame_LeftPanel tr[data-router-action="filter:toggle-row"]').first.click()
        page.locator('#frame_LeftPanel button[data-router-action="filter:apply"]').click()
        page.wait_for_function("() => window.__applyFilterCalled === true", timeout=10_000)


class TestStep16ClinicalTrialsThreeFrames:
    """ClientRouter.getStreamedPayloads(op:'clinical_trials_page', payload:{condition})
    → SS streams kind:'trials' → ClinicalTrialsWidget paints LeftPanel +
    MainWindow + RightPanel."""

    def test_clinical_trials_paints_three_frames(self, page):
        page.goto("https://localhost/", wait_until="networkidle")
        page.wait_for_function(
            "() => window.ClientRouter && document.querySelector('#frame_MainWindow') != null",
            timeout=15_000,
        )
        page.evaluate("""() => {
            window.ClientRouter.getStreamedPayloads({
                op: 'clinical_trials_page',
                payload: { condition: 'diabetes', page: 1 },
            });
        }""")
        page.wait_for_function(
            "() => /NCT\\d+/.test(document.querySelector('#frame_MainWindow')?.innerText || '')",
            timeout=60_000,
        )
        mw = page.locator('#frame_MainWindow').inner_text()
        assert _nct_ids(mw), f"No NCT id in MainWindow: {mw[:300]}"
        # LeftPanel + RightPanel get the trial chrome too (per ClinicalTrialsWidget design).
        assert len(page.locator('#frame_LeftPanel').inner_text()) > 0
        assert len(page.locator('#frame_RightPanel').inner_text()) > 0
