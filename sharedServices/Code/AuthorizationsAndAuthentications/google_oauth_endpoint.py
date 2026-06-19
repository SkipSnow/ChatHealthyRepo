# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Google OAuth handshake - EPIC-002-F-003-S-004.
#
# Wire:
#   GET /auth/google/start      - 302 to Google consent screen.
#   GET /auth/google/callback   - exchange code, fetch sub, register-or-
#                                 find users record via the A&A tool,
#                                 set ChatHealthyUserCookie, 302 back to
#                                 the wrapper origin.
#
# Required HF Space secrets per env:
#   GOOGLE_OAUTH_CLIENT_ID
#   GOOGLE_OAUTH_CLIENT_SECRET

from __future__ import annotations

import os
import json
import time
import hmac
import base64
import hashlib
from chathealthy_frontend_lib import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException
import secrets
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

log = ChatHealthyLoggingService()


GOOGLE_AUTHZ_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# After Google redirects back, the callback lands on the SharedServices
# Space directly (so the Space's origin can set ChatHealthyUserCookie).
# The Google Cloud OAuth client must list these URLs in its Authorized
# Redirect URIs.
ENV_TO_REDIRECT_URI = {
    "dev":   "https://skipsnow-dev-sharedservicesspace.hf.space/auth/google/callback",
    "qa":    "https://skipsnow-qa-sharedservicesspace.hf.space/auth/google/callback",
    "prod":  "https://skipsnow-sharedservicesspace.hf.space/auth/google/callback",
    "local": "https://localhost:8002/auth/google/callback",
}

# Where the post-OAuth flow lands the user in the wrapper site.
ENV_TO_WRAPPER_ORIGIN = {
    "dev":   "https://dev.chathealthy.ai",
    "qa":    "https://qa.chathealthy.ai",
    "prod":  "https://chathealthy.ai",
    "local": "https://localhost",
}

CHATHEALTHY_USER_COOKIE_NAME = "ChatHealthyRegisteredUserCookie"
CHATHEALTHY_USER_COOKIE_MAX_AGE = 5184000  # 60 days, per S-006-REQ-T-001
CHATHEALTHY_USER_COOKIE_DOMAIN = "chathealthy.ai"
CHATHEALTHY_USER_COOKIE_PATH = "/gate"

# State param TTL.
STATE_TTL_SECONDS = 600


def wrapper_origin(server_env: str) -> str:
    origin = ENV_TO_WRAPPER_ORIGIN.get(server_env)
    if not origin:
        raise HTTPException(500, f"Unknown server_env: {server_env!r}")
    return origin


def redirect_uri(server_env: str) -> str:
    uri = ENV_TO_REDIRECT_URI.get(server_env)
    if not uri:
        raise HTTPException(500, f"Unknown server_env: {server_env!r}")
    return uri


def client_id() -> str:
    cid = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    if not cid:
        raise HTTPException(500, "GOOGLE_OAUTH_CLIENT_ID is not configured on this Space")
    return cid


def client_secret() -> str:
    sec = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    if not sec:
        raise HTTPException(500, "GOOGLE_OAUTH_CLIENT_SECRET is not configured on this Space")
    return sec


def state_signing_key() -> bytes:
    return client_secret().encode("utf-8")


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def build_state(server_env: str, session_guid: Optional[str] = None) -> str:
    payload = {
        "env":   server_env,
        "nonce": secrets.token_urlsafe(16),
        "ts":    int(time.time()),
    }
    # Carry the caller's 32-char session_guid through Google so the callback
    # can rebind the OAuth identity to the originating guest session even
    # when ch_session can't ride to the SharedServices Space host (cookie
    # is Domain=chathealthy.ai; Path=/gate, so it never reaches the HF host).
    # The state body is HMAC-signed with the client secret, so the GUID
    # round-tripping through Google can't be forged.
    if session_guid:
        payload["session_guid"] = session_guid
    body = b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = b64url(hmac.new(state_signing_key(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_state(state: str) -> dict:
    try:
        body_b64, sig_b64 = state.split(".", 1)
    except ValueError as _exc:
        log.warning("OAuth state malformed: %s", _exc, exc=ChatHealthyException(
                                                        mode="oauth_state_malformed",
                                                        message="OAuth state parameter malformed (missing '.' separator)",
                                                        component="GoogleOAuthEndpoint",
                                                        exception=_exc,
                                                    ), if_not_debug_log=True)
        raise HTTPException(400, "state parameter malformed")
    expected_sig = b64url(hmac.new(state_signing_key(), body_b64.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected_sig, sig_b64):
        raise HTTPException(400, "state parameter signature invalid")
    try:
        payload = json.loads(b64url_decode(body_b64).decode("utf-8"))
    except Exception as _exc:
        log.warning("OAuth state body invalid: %s", _exc, exc=ChatHealthyException(
                                                           mode="oauth_state_body_invalid",
                                                           message=f"OAuth state body invalid: {_exc}",
                                                           component="GoogleOAuthEndpoint",
                                                           exception=_exc,
                                                       ), if_not_debug_log=True)
        raise HTTPException(400, "state parameter body invalid")
    if int(time.time()) - int(payload.get("ts", 0)) > STATE_TTL_SECONDS:
        raise HTTPException(400, "state parameter expired")
    return payload


def set_chathealthy_user_cookie(response: RedirectResponse, user_id: str) -> None:
    response.set_cookie(
        key=CHATHEALTHY_USER_COOKIE_NAME,
        value=user_id,
        max_age=CHATHEALTHY_USER_COOKIE_MAX_AGE,
        secure=True,
        httponly=True,
        samesite="lax",
        domain=CHATHEALTHY_USER_COOKIE_DOMAIN,
        path=CHATHEALTHY_USER_COOKIE_PATH,
    )


async def _invoke_oauth_login_tool(
    server_env: str,
    session_guid: str,
    *,
    phase: str,
    oauth_code: Optional[str] = None,
    oauth_state: Optional[str] = None,
    flow: str = "login",
):
    from authentication.oauth_login_tool import (
        TOOL as OAUTH_LOGIN_TOOL,
        Request as OAuthLoginRequest,
    )
    from authentication.authorizations_and_authentications_tool import (
        get_mongo_frontend,
    )
    from authentication.agent_deps import AgentDeps
    from authentication.user_object import UserObject
    from datetime import datetime, timezone, timedelta

    deps = AgentDeps(
        user_object=UserObject(
            current_session_token="NULL",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        ),
        session_token=None,
        mongo_frontend=get_mongo_frontend(),
        server_env=server_env,
        stream=lambda _evt: None,
    )
    req = OAuthLoginRequest(
        phase=phase,
        identity_provider="Google",
        server_env=server_env,
        session_guid=session_guid,
        oauth_code=oauth_code,
        oauth_state=oauth_state,
        flow=flow,
    )
    return await OAUTH_LOGIN_TOOL.run(deps, req)


def _build_popup_close_html(
    *, outcome: str, message: str, email: Optional[str], user_id: Optional[str],
) -> "HTMLResponse":
    """Return an HTML page that posts the outcome back to the opener
    window and closes the popup. Used to land OAuth callback results into
    the wrapper while keeping the popup pattern symmetrical with how real
    Google OAuth popup flows return control to the parent window."""
    from fastapi.responses import HTMLResponse
    import json as _json
    body = _json.dumps({
        "type": "oauth_login_result",
        "outcome": outcome,
        "message": message,
        "email": email,
        "user_id": user_id,
    })
    safe_message = (message or "").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Signing in...</title>
<style>body{{font-family:Roboto,Arial,sans-serif;text-align:center;padding:3em;color:#202124;}}</style>
</head><body>
<p data-testid="oauth-popup-message">{safe_message}</p>
<p style="color:#5f6368;">This window will close in a moment.</p>
<script>
(function() {{
  var payload = {body};
  try {{
    if (window.opener && !window.opener.closed) {{
      window.opener.postMessage(payload, '*');
    }}
  }} catch (e) {{ /* swallow */ }}
  setTimeout(function() {{ window.close(); }}, 500);
}})();
</script>
</body></html>"""
    return HTMLResponse(html)


def _wrap_invoke_into_html(
    landing_url: str, user_id: Optional[str],
    *, outcome: str, message: str, email: Optional[str],
):
    response = _build_popup_close_html(
        outcome=outcome, message=message, email=email, user_id=user_id,
    )
    if user_id:
        set_chathealthy_user_cookie(response, user_id)
    return response


class GoogleOAuthEndpoint:
    """FastAPI shell. All OAuth logic routes through OAuthLoginTool."""

    @staticmethod
    async def start(
        server_env: str,
        session_guid: Optional[str] = None,
        flow: str = "login",
    ) -> RedirectResponse:
        if not session_guid:
            session_guid = secrets.token_hex(16)
        resp = await _invoke_oauth_login_tool(
            server_env, session_guid, phase="start", flow=flow,
        )
        if resp.outcome != "redirect" or not resp.authz_url:
            return _build_popup_close_html(
                outcome="fail",
                message="OAuth start failed",
                email=None, user_id=None,
            )
        return RedirectResponse(resp.authz_url, status_code=302)

    @staticmethod
    async def callback(
        *,
        code: Optional[str],
        state: Optional[str],
        server_env: str,
        session_guid: Optional[str],
        error: Optional[str] = None,
    ):
        def _fail_popup(tag: str, message: str):
            log.warning("OAUTH-CALLBACK fail tag=%s", tag)
            return _build_popup_close_html(
                outcome="fail", message=message, email=None, user_id=None,
            )

        if error:
            return _fail_popup(error, f"Google returned error: {error}")
        if not code or not state:
            return _fail_popup(
                "missing_code_or_state",
                "OAuth callback missing code or state parameter",
            )

        try:
            state_payload = verify_state(state)
        except HTTPException as e:
            log.warning(
                "OAUTH-CALLBACK state_invalid detail=%s",
                e.detail,
                exc=ChatHealthyException(
                    mode="oauth_state_verification_failed",
                    message=f"OAuth state verification failed: {e.detail}",
                    component="GoogleOAuthEndpoint",
                    exception=e,
                ),
                if_not_debug_log=True,
            )
            return _fail_popup(
                "state_invalid", "OAuth state verification failed",
            )

        state_session_guid = state_payload.get("session_guid")
        if state_session_guid:
            session_guid = state_session_guid
        if not session_guid:
            return _fail_popup(
                "missing_session_cookie",
                "OAuth callback missing session identifier",
            )

        resp = await _invoke_oauth_login_tool(
            server_env, session_guid,
            phase="callback", oauth_code=code, oauth_state=state,
        )
        return _wrap_invoke_into_html(
            landing_url=wrapper_origin(server_env),
            user_id=(resp.user_id if resp.outcome == "success" else None),
            outcome=resp.outcome,
            message=resp.user_facing_message,
            email=resp.public_username,
        )
