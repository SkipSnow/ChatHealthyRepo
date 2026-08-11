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
from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
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
    "dev":   "https://dev-hf.chathealthy.ai/auth/google/callback",
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
        # Mode 2 (REQ-B-008): malformed OAuth state → user can't auth.
        # log.error always; the HTTPException(400) below surfaces the
        # failure to the caller for user-facing handling.
        log.error("OAuth state malformed: %s", _exc, exc=ChatHealthyException(
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
        # Mode 2 (REQ-B-008): OAuth state body invalid → user can't auth.
        log.error("OAuth state body invalid: %s", _exc, exc=ChatHealthyException(
                                                           mode="oauth_state_body_invalid",
                                                           message=f"OAuth state body invalid: {_exc}",
                                                           component="GoogleOAuthEndpoint",
                                                           exception=_exc,
                                                       ), if_not_debug_log=True)
        raise HTTPException(400, "state parameter body invalid")
    if int(time.time()) - int(payload.get("ts", 0)) > STATE_TTL_SECONDS:
        raise HTTPException(400, "state parameter expired")
    return payload


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
    from chathealthy_lib.authentication.agent_deps import AgentDeps
    from chathealthy_lib.authentication.user_object import UserObject
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


def _popup_close_redirect(server_env: str) -> RedirectResponse:
    """Redirect the popup back to the wrapper origin with a query flag
    the wrapper page reads to close itself. No HTML in this module —
    display semantics (including the window.close call) live in the
    wrapper page's React-driven layer."""
    target = wrapper_origin(server_env) + "/?oauth_popup_close=1"
    return RedirectResponse(target, status_code=302)


def _persist_oauth_result(
    session_guid: str, *,
    outcome: str, message: str, email: Optional[str], user_id: Optional[str],
) -> None:
    """Stash the OAuth result on the user_object so the React
    HeaderWidget can poll it via claim_oauth_result and render the banner.
    No HTML in this path — pure structured data."""
    from authentication.authorizations_and_authentications_tool import (
        get_mongo_frontend, SESSION_DB, SESSION_COLLECTION,
    )
    coll = get_mongo_frontend()[SESSION_DB][SESSION_COLLECTION]
    coll.update_one(
        {"_id": session_guid},
        {"$set": {"pending_oauth_result": {
            "outcome": outcome,
            "message": message,
            "email": email,
            "user_id": user_id,
        }}},
        upsert=False,
    )
    log.info(
        "OAUTH-PERSIST result session_prefix=%s outcome=%s email=%s",
        session_guid[:8] + "...", outcome, email,
    )


class GoogleOAuthEndpoint:
    """FastAPI shell. All OAuth logic routes through OAuthLoginTool."""

    @staticmethod
    async def start(
        server_env: str,
        session_guid: Optional[str] = None,
        flow: str = "login",
    ) -> RedirectResponse:
        # Audit: distinguish a wrapper that sent a real session_guid from
        # one that sent empty/none and forced the server to synthesize.
        # The synthesize case is a wrapper-side bug — the callback will
        # later raise oauth_login_no_session_for_callback because the
        # synthesized GUID has no admin.sessions row.
        client_supplied = bool(session_guid)
        client_guid_len = len(session_guid or "")
        if not session_guid:
            session_guid = secrets.token_hex(16)
        log.info(
            "OAUTH-START session_guid client_supplied=%s client_len=%d "
            "used_prefix=%s flow=%s server_env=%s",
            client_supplied, client_guid_len,
            session_guid[:8] + "...", flow, server_env,
        )
        resp = await _invoke_oauth_login_tool(
            server_env, session_guid, phase="start", flow=flow,
        )
        if resp.outcome != "redirect" or not resp.authz_url:
            _persist_oauth_result(
                session_guid, outcome="fail",
                message="OAuth start failed", email=None, user_id=None,
            )
            return _popup_close_redirect(server_env)
        return RedirectResponse(resp.authz_url, status_code=302)

    @staticmethod
    async def callback(
        *,
        code: Optional[str],
        state: Optional[str],
        server_env: str,
        error: Optional[str] = None,
    ):
        def _fail_popup(tag: str, message: str, session_guid: Optional[str]):
            log.warning("OAUTH-CALLBACK fail tag=%s", tag)
            if session_guid:
                _persist_oauth_result(
                    session_guid, outcome="fail",
                    message=message, email=None, user_id=None,
                )
            return _popup_close_redirect(server_env)

        if error:
            return _fail_popup(error, f"Google returned error: {error}", None)
        if not code or not state:
            return _fail_popup(
                "missing_code_or_state",
                "OAuth callback missing code or state parameter",
                None,
            )

        try:
            state_payload = verify_state(state)
        except HTTPException as e:
            log.error(
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
                "state_invalid", "OAuth state verification failed", None,
            )

        session_guid = state_payload.get("session_guid")
        if not session_guid:
            return _fail_popup(
                "missing_session_guid",
                "OAuth callback state did not carry a session identifier",
                None,
            )

        resp = await _invoke_oauth_login_tool(
            server_env, session_guid,
            phase="callback", oauth_code=code, oauth_state=state,
        )
        _persist_oauth_result(
            session_guid,
            outcome=resp.outcome,
            message=resp.user_facing_message,
            email=resp.public_username,
            user_id=(resp.user_id if resp.outcome == "success" else None),
        )
        return _popup_close_redirect(server_env)
