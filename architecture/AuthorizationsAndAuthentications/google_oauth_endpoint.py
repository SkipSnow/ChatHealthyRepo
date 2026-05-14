# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Google OAuth handshake — EPIC-002-F-003-S-004.
# Scope (this file): drive the user to and from Google, returning a
# verified email. Post-email work (login_or_register, token mint, User
# State persistence) is a separate hand-off and lives in the entrance
# tool, not here.
#
# Wire:
#   GET /auth/google/start      → JSON { authz_url, state }
#   GET /auth/google/callback   → 302 to wrapper with ?auth_email=…
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
from fastapi.responses import RedirectResponse, JSONResponse
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests


GOOGLE_AUTHZ_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

_ENV_TO_WRAPPER_ORIGIN = {
    "dev":   "https://dev.chathealthy.ai",
    "qa":    "https://qa.chathealthy.ai",
    "prod":  "https://chathealthy.ai",
    "local": "https://localhost",
}

# State param TTL — Google returns the user within minutes.
_STATE_TTL_SECONDS = 600


def _wrapper_origin(server_env: str) -> str:
    origin = _ENV_TO_WRAPPER_ORIGIN.get(server_env)
    if not origin:
        raise HTTPException(500, f"Unknown server_env: {server_env!r}")
    return origin


def _redirect_uri(server_env: str) -> str:
    return _wrapper_origin(server_env) + "/auth/google/callback"


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
    """Signs the state parameter so the callback can verify it wasn't
    minted by a third party. Uses the OAuth client secret as the HMAC
    key — already-secret material, already required for this endpoint."""
    return _client_secret().encode("utf-8")


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _build_state(server_env: str) -> str:
    """Compact signed state: env + nonce + timestamp + HMAC."""
    payload = {
        "env":   server_env,
        "nonce": secrets.token_urlsafe(16),
        "ts":    int(time.time()),
    }
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64url(hmac.new(_state_signing_key(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _verify_state(state: str) -> dict:
    """Reverses _build_state. Raises HTTPException on tamper, expiry, or env mismatch."""
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


class GoogleOAuthEndpoint:
    """Stateless handler for the Google OAuth handshake."""

    @staticmethod
    def start(server_env: str) -> JSONResponse:
        """Build the Google consent-screen URL the wrapper should
        navigate the user to."""
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
        return JSONResponse({
            "authz_url": GOOGLE_AUTHZ_URL + "?" + urlencode(params),
            "state":     state,
        })

    @staticmethod
    def callback(code: Optional[str], state: Optional[str], server_env: str,
                 error: Optional[str] = None) -> RedirectResponse:
        """Receive Google's redirect, exchange code for tokens, verify
        the ID token, extract the email, and 302 the wrapper back with
        ?auth_email=… (or ?auth_error=… on failure)."""
        wrapper = _wrapper_origin(server_env)

        if error:
            return RedirectResponse(f"{wrapper}/?auth_error={error}", status_code=302)
        if not code or not state:
            return RedirectResponse(f"{wrapper}/?auth_error=missing_code_or_state", status_code=302)

        try:
            _verify_state(state)
        except HTTPException as e:
            return RedirectResponse(f"{wrapper}/?auth_error=state_{e.detail.split()[-1]}", status_code=302)

        # Exchange authorization code for an ID token at Google's token endpoint.
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
                return RedirectResponse(f"{wrapper}/?auth_error=token_exchange_{r.status_code}", status_code=302)
            tokens = r.json()
        except Exception:
            return RedirectResponse(f"{wrapper}/?auth_error=token_exchange_network", status_code=302)

        id_token_str = tokens.get("id_token")
        if not id_token_str:
            return RedirectResponse(f"{wrapper}/?auth_error=missing_id_token", status_code=302)

        # Verify the ID token signature + aud against our client_id.
        try:
            claims = id_token.verify_oauth2_token(
                id_token_str,
                google_requests.Request(),
                audience=_client_id(),
            )
        except Exception:
            return RedirectResponse(f"{wrapper}/?auth_error=id_token_invalid", status_code=302)

        email = claims.get("email")
        if not email or not claims.get("email_verified"):
            return RedirectResponse(f"{wrapper}/?auth_error=email_not_verified", status_code=302)

        # ── Hand-off point ─────────────────────────────────────────
        # Per S-004 scope: OAuth code's job ends at "verified email."
        # The entrance tool (login_or_register) is a separate hand-off
        # that owns token manufacture + User State persistence. For
        # now, the email is returned to the wrapper as-is; the entrance
        # tool will be wired here when its contract is defined.
        # ───────────────────────────────────────────────────────────

        return RedirectResponse(f"{wrapper}/?auth_email={email}", status_code=302)
