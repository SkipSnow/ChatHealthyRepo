# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Local-only fake Google sign-in page. Single endpoint, no IdP mimicry."""
from __future__ import annotations

import base64
from typing import Optional

from fastapi.responses import HTMLResponse, RedirectResponse


FAKE_CODE_PREFIX = "fake_local_"


def auth_page_html(state: str, flow: str) -> str:
    register_checked = "checked" if flow == "register" else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>LOCAL FAKE — ChatHealthy stand-in for Google sign-in</title>
<style>
 body {{ font-family: Arial, sans-serif; padding: 2em; max-width: 30em; margin: 0 auto; color: #202124; }}
 .badge {{ background: #fef7e0; border: 0.0625em solid #f9ab00; padding: 0.5em 1em; border-radius: 0.25em; font-size: 0.85em; margin-bottom: 1.5em; }}
 h1 {{ font-size: 1.2em; margin-bottom: 1em; }}
 label {{ display: block; margin: 0.75em 0 0.25em; font-size: 0.9em; }}
 input[type=email], input[type=password] {{ width: 100%; padding: 0.5em; box-sizing: border-box; border: 0.0625em solid #999; border-radius: 0.25em; }}
 .check-row {{ margin: 1em 0; }}
 button {{ background: #0b7a75; color: #fff; border: none; border-radius: 0.25em; padding: 0.625em 1.25em; font-size: 0.95em; cursor: pointer; margin-top: 1em; }}
</style></head><body>
 <div class="badge"><strong>LOCAL FAKE</strong> — not Google. Served by SharedServices on localhost only.</div>
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
