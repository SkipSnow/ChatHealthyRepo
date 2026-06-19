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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


FAKE_CODE_PREFIX = "fake_local_"
LOCAL_FAKE_KID = "local-fake-key-1"
LOCAL_FAKE_SHARED_SECRET = secrets.token_urlsafe(32)
LOCAL_FAKE_ISSUER = "https://accounts.google.com"
ID_TOKEN_TTL_SECONDS = 3600


def auth_page_html(state: str, flow: str) -> str:
    register_checked = "checked" if flow == "register" else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>LOCAL FAKE - ChatHealthy stand-in for Google sign-in</title>
<style>
 body {{ font-family: Arial, sans-serif; padding: 2em; max-width: 30em; margin: 0 auto; color: #202124; }}
 .badge {{ background: #fef7e0; border: 0.0625em solid #f9ab00; padding: 0.5em 1em; border-radius: 0.25em; font-size: 0.85em; margin-bottom: 1.5em; }}
 h1 {{ font-size: 1.2em; margin-bottom: 1em; }}
 label {{ display: block; margin: 0.75em 0 0.25em; font-size: 0.9em; }}
 input[type=email], input[type=password] {{ width: 100%; padding: 0.5em; box-sizing: border-box; border: 0.0625em solid #999; border-radius: 0.25em; }}
 .check-row {{ margin: 1em 0; }}
 button {{ background: #0b7a75; color: #fff; border: none; border-radius: 0.25em; padding: 0.625em 1.25em; font-size: 0.95em; cursor: pointer; margin-top: 1em; }}
</style></head><body>
 <div class="badge"><strong>LOCAL FAKE</strong> - not Google. Served by SharedServices on localhost only.</div>
 <h1>Sign in as a ChatHealthy user</h1>
 <form method="POST" action="/fake_google/submit" data-testid="fake-google-form">
  <input type="hidden" name="state" value="{state}">
  <input type="hidden" name="flow" value="{flow}">
  <label for="email">Email</label>
  <input type="email" name="email" id="email" data-testid="fake-google-email" required>
  <label for="password">Password</label>
  <input type="password" name="password" id="password" data-testid="fake-google-password" required>
  <div class="check-row">
   <label><input type="checkbox" name="create_account" data-testid="fake-google-register-checkbox" {register_checked}> Create a new account</label>
  </div>
  <button type="submit" data-testid="fake-google-submit-button">Submit</button>
 </form>
</body></html>"""


def serve_auth_page(state: str, flow: str = "login") -> HTMLResponse:
    return HTMLResponse(auth_page_html(state=state, flow=flow))


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
