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
import secrets
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests


GOOGLE_AUTHZ_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# After Google redirects back, the callback lands on the SharedServices
# Space (NOT the wrapper) so it can set ChatHealthyUserCookie on the
# SharedServices origin. The Google Cloud OAuth client must list these
# URLs in its Authorized Redirect URIs.
_ENV_TO_REDIRECT_URI = {
    "dev":   "https://skipsnow-dev-sharedservicesspace.hf.space/auth/google/callback",
    "qa":    "https://skipsnow-qa-sharedservicesspace.hf.space/auth/google/callback",
    "prod":  "https://skipsnow-sharedservicesspace.hf.space/auth/google/callback",
    "local": "https://localhost:8002/auth/google/callback",
}

# Where the post-OAuth flow lands the user in the wrapper site.
_ENV_TO_WRAPPER_ORIGIN = {
    "dev":   "https://dev.chathealthy.ai",
    "qa":    "https://qa.chathealthy.ai",
    "prod":  "https://chathealthy.ai",
    "local": "https://localhost",
}

# ChatHealthyUserCookie - per S-004-REQ-T-011.
CHATHEALTHY_USER_COOKIE_NAME = "ChatHealthyUserCookie"
CHATHEALTHY_USER_COOKIE_MAX_AGE = 60 * 24 * 60 * 60  # 60 days in seconds

# State param TTL.
_STATE_TTL_SECONDS = 600


def _wrapper_origin(server_env: str) -> str:
    origin = _ENV_TO_WRAPPER_ORIGIN.get(server_env)
    if not origin:
        raise HTTPException(500, f"Unknown server_env: {server_env!r}")
    return origin


def _redirect_uri(server_env: str) -> str:
    uri = _ENV_TO_REDIRECT_URI.get(server_env)
    if not uri:
        raise HTTPException(500, f"Unknown server_env: {server_env!r}")
    return uri


def _client_id() -> str:
    cid = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    if not cid:
        raise HTTPException(500, "GOOGLE_OAUTH_CLIENT_ID is not configured on this Space")
    return cid


def _client_secret() -> str:
    sec = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    if not sec:
        raise HTTPException(500, "GOOGLE_OAUTH_CLIENT_SECRET is not configured on this Space")
    return sec


def _state_signing_key() -> bytes:
    return _client_secret().encode("utf-8")


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _build_state(server_env: str) -> str:
    payload = {
        "env":   server_env,
        "nonce": secrets.token_urlsafe(16),
        "ts":    int(time.time()),
    }
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64url(hmac.new(_state_signing_key(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _verify_state(state: str) -> dict:
    try:
        body_b64, sig_b64 = state.split(".", 1)
    except ValueError:
        raise HTTPException(400, "state parameter malformed")
    expected_sig = _b64url(hmac.new(_state_signing_key(), body_b64.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected_sig, sig_b64):
        raise HTTPException(400, "state parameter signature invalid")
    try:
        payload = json.loads(_b64url_decode(body_b64).decode("utf-8"))
    except Exception:
        raise HTTPException(400, "state parameter body invalid")
    if int(time.time()) - int(payload.get("ts", 0)) > _STATE_TTL_SECONDS:
        raise HTTPException(400, "state parameter expired")
    return payload


def _set_chathealthy_user_cookie(response: RedirectResponse, user_id: str) -> None:
    response.set_cookie(
        key=CHATHEALTHY_USER_COOKIE_NAME,
        value=user_id,
        max_age=CHATHEALTHY_USER_COOKIE_MAX_AGE,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


class GoogleOAuthEndpoint:
    """Stateless handler for the Google OAuth handshake."""

    @staticmethod
    def start(server_env: str) -> RedirectResponse:
        """302 the browser to Google's consent screen."""
        state = _build_state(server_env)
        params = {
            "client_id":     _client_id(),
            "redirect_uri":  _redirect_uri(server_env),
            "response_type": "code",
            "scope":         "openid email",
            "state":         state,
            "prompt":        "select_account",
            "access_type":   "online",
        }
        return RedirectResponse(
            GOOGLE_AUTHZ_URL + "?" + urlencode(params),
            status_code=302,
        )

    @staticmethod
    def callback(
        *,
        code: Optional[str],
        state: Optional[str],
        server_env: str,
        session_guid: Optional[str],
        error: Optional[str] = None,
    ) -> RedirectResponse:
        """Receive Google's redirect, exchange code, register-or-find the
        users record, set ChatHealthyUserCookie, then 302 back to the
        wrapper origin."""
        wrapper = _wrapper_origin(server_env)

        def _error_redirect(tag: str) -> RedirectResponse:
            return RedirectResponse(f"{wrapper}/?auth_error={tag}", status_code=302)

        if error:
            return _error_redirect(error)
        if not code or not state:
            return _error_redirect("missing_code_or_state")
        if not session_guid:
            return _error_redirect("missing_session_cookie")

        try:
            _verify_state(state)
        except HTTPException as e:
            return _error_redirect(f"state_{e.detail.split()[-1]}")

        try:
            with httpx.Client(timeout=10.0) as client:
                r = client.post(GOOGLE_TOKEN_URL, data={
                    "code":          code,
                    "client_id":     _client_id(),
                    "client_secret": _client_secret(),
                    "redirect_uri":  _redirect_uri(server_env),
                    "grant_type":    "authorization_code",
                })
            if r.status_code != 200:
                return _error_redirect(f"token_exchange_{r.status_code}")
            tokens = r.json()
        except Exception:
            return _error_redirect("token_exchange_network")

        id_token_str = tokens.get("id_token")
        if not id_token_str:
            return _error_redirect("missing_id_token")

        try:
            claims = id_token.verify_oauth2_token(
                id_token_str,
                google_requests.Request(),
                audience=_client_id(),
            )
        except Exception:
            return _error_redirect("id_token_invalid")

        google_sub = claims.get("sub")
        email = claims.get("email")
        if not google_sub:
            return _error_redirect("missing_sub")
        if not email or not claims.get("email_verified"):
            return _error_redirect("email_not_verified")

        # Hand off to the A&A tool to register-or-find the users record.
        from authentication.authorizations_and_authentications_tool import (
            TOOL as AUTHN_TOOL,
            get_mongo_frontend,
        )
        try:
            user_id = AUTHN_TOOL.handle_oauth_login(
                get_mongo_frontend(),
                google_sub=google_sub,
                session_guid=session_guid,
            )
        except Exception as exc:
            return _error_redirect(f"register_failed_{type(exc).__name__}")

        redirect = RedirectResponse(
            f"{wrapper}/?auth_email={email}",
            status_code=302,
        )
        _set_chathealthy_user_cookie(redirect, user_id)
        return redirect
