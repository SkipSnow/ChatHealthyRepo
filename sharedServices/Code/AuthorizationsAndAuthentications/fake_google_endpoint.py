# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Local-only fake Google IdP.

Two endpoints together replace Google's authorization + token endpoints
on local. The fake mints REAL signed JWTs (HS256 with a process-local
secret) so the callback path uses the same id_token verification flow
as production. No claim-fabrication logic in the auth tool — the fake
IS the IdP."""
from __future__ import annotations

import base64
import secrets
import time

import jwt
import urllib.parse
from fastapi.responses import JSONResponse, RedirectResponse


FAKE_CODE_PREFIX = "fake_local_"
LOCAL_FAKE_KID = "local-fake-key-1"
LOCAL_FAKE_SHARED_SECRET = secrets.token_urlsafe(32)
LOCAL_FAKE_ISSUER = "https://accounts.google.com"
ID_TOKEN_TTL_SECONDS = 3600

# Wrapper origin for local. The fake auth flow redirects to the wrapper
# where the React FakeGoogleLoginWidget renders the form. No HTML in
# this module — all display lives in React per the firm's architecture.
LOCAL_WRAPPER_ORIGIN = "https://localhost"


def serve_auth_page(state: str, flow: str = "login") -> RedirectResponse:
    """Redirect the popup to the wrapper page with the OAuth state in
    query params. React (FakeGoogleLoginWidget) reads the params and
    renders the local fake-Google sign-in form. Form submission posts
    back to /fake_google/submit on this Space."""
    qs = urllib.parse.urlencode({
        "fake_google_login": "1",
        "state": state,
        "flow": flow,
    })
    return RedirectResponse(f"{LOCAL_WRAPPER_ORIGIN}/?{qs}", status_code=302)


def submit_credentials(
    *,
    email: str,
    password: str,
    state: str,
    flow: str,
    server_env: str,
) -> RedirectResponse:
    if server_env != "local":
        return RedirectResponse("/?fake_only_on_local", status_code=302)
    payload = base64.urlsafe_b64encode(email.encode("utf-8")).rstrip(b"=").decode("ascii")
    fake_code = FAKE_CODE_PREFIX + payload
    callback_url = (
        f"https://localhost:8002/auth/google/callback"
        f"?code={fake_code}&state={state}"
    )
    return RedirectResponse(callback_url, status_code=302)


def _decode_fake_code(code: str) -> str:
    if not code or not code.startswith(FAKE_CODE_PREFIX):
        return ""
    payload = code[len(FAKE_CODE_PREFIX):]
    pad = "=" * (-len(payload) % 4)
    try:
        return base64.urlsafe_b64decode(payload + pad).decode("utf-8")
    except Exception:
        return ""


def _stable_sub(email: str) -> str:
    sub_int = abs(hash("google-sub-fixture::" + email.lower())) % (10**21)
    return str(sub_int).zfill(21)


def exchange_code_for_token(
    *,
    code: str,
    client_id: str,
    server_env: str,
) -> JSONResponse:
    if server_env != "local":
        return JSONResponse({"error": "fake_only_on_local"}, status_code=404)
    email = _decode_fake_code(code)
    if not email:
        return JSONResponse({"error": "invalid_code"}, status_code=400)
    now = int(time.time())
    payload = {
        "iss": LOCAL_FAKE_ISSUER,
        "azp": client_id,
        "aud": client_id,
        "sub": _stable_sub(email),
        "email": email,
        "email_verified": True,
        "iat": now,
        "exp": now + ID_TOKEN_TTL_SECONDS,
    }
    id_token = jwt.encode(
        payload,
        LOCAL_FAKE_SHARED_SECRET,
        algorithm="HS256",
        headers={"kid": LOCAL_FAKE_KID},
    )
    return JSONResponse({
        "id_token": id_token,
        "access_token": secrets.token_urlsafe(32),
        "token_type": "Bearer",
        "expires_in": ID_TOKEN_TTL_SECONDS,
        "scope": "openid email",
    })
