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


class GoogleOAuthEndpoint:
    """Stateless handler for the Google OAuth handshake."""

    @staticmethod
    def start(server_env: str, session_guid: Optional[str] = None) -> RedirectResponse:
        """302 the browser to Google's consent screen.

        `session_guid` is the caller's 32-char guest-session GUID, supplied
        by the /gate redirect when the user clicks Login & Registration.
        It is baked into the signed `state` parameter so the callback can
        recover it even though the ch_session cookie (Domain=chathealthy.ai;
        Path=/gate) does not ride to the HF Space callback host.
        """
        state = build_state(server_env, session_guid=session_guid)
        params = {
            "client_id":     client_id(),
            "redirect_uri":  redirect_uri(server_env),
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
        wrapper = wrapper_origin(server_env)
        log.info(
            "OAUTH-CALLBACK entry env=%s cookie_session_guid=%s state_present=%s code_present=%s error=%s",
            server_env, (session_guid[:8] + "..." if session_guid else None),
            bool(state), bool(code), error,
        )

        def _error_redirect(tag: str) -> RedirectResponse:
            log.warning("OAUTH-CALLBACK error_redirect tag=%s", tag)
            return RedirectResponse(f"{wrapper}/?auth_error={tag}", status_code=302)

        if error:
            return _error_redirect(error)
        if not code or not state:
            return _error_redirect("missing_code_or_state")

        try:
            state_payload = verify_state(state)
        except HTTPException as e:
            log.warning("OAuth state verification failed: %s", e.detail, exc=ChatHealthyException(
                                                                          mode="oauth_state_verification_failed",
                                                                          message=f"OAuth state verification failed: {e.detail}",
                                                                          component="GoogleOAuthEndpoint",
                                                                          exception=e,
                                                                      ), if_not_debug_log=True)
            return _error_redirect(f"state_{e.detail.split()[-1]}")

        state_session_guid = state_payload.get("session_guid")
        if state_session_guid:
            session_guid = state_session_guid
        log.info(
            "OAUTH-CALLBACK state_verified state_session_guid=%s effective_session_guid=%s",
            (state_session_guid[:8] + "..." if state_session_guid else None),
            (session_guid[:8] + "..." if session_guid else None),
        )
        if not session_guid:
            return _error_redirect("missing_session_cookie")

        try:
            with httpx.Client(timeout=10.0) as client:
                r = client.post(GOOGLE_TOKEN_URL, data={
                    "code":          code,
                    "client_id":     client_id(),
                    "client_secret": client_secret(),
                    "redirect_uri":  redirect_uri(server_env),
                    "grant_type":    "authorization_code",
                })
            if r.status_code != 200:
                return _error_redirect(f"token_exchange_{r.status_code}")
            tokens = r.json()
        except Exception as _exc:
            log.error("OAuth token exchange network failed: %s", _exc, exc=ChatHealthyException(
                                                                        mode="oauth_token_exchange_network_failed",
                                                                        message=f"OAuth token exchange network failed: {_exc}",
                                                                        component="GoogleOAuthEndpoint",
                                                                        exception=_exc,
                                                                    ), if_not_debug_log=True)
            return _error_redirect("token_exchange_network")

        id_token_str = tokens.get("id_token")
        if not id_token_str:
            return _error_redirect("missing_id_token")

        try:
            claims = id_token.verify_oauth2_token(
                id_token_str,
                google_requests.Request(),
                audience=client_id(),
            )
        except Exception as _exc:
            log.warning("OAuth id_token validation failed: %s", _exc, exc=ChatHealthyException(
                                                                       mode="oauth_id_token_invalid",
                                                                       message=f"OAuth id_token validation failed: {_exc}",
                                                                       component="GoogleOAuthEndpoint",
                                                                       exception=_exc,
                                                                   ), if_not_debug_log=True)
            return _error_redirect("id_token_invalid")

        google_sub = claims.get("sub")
        email = claims.get("email")
        if not google_sub:
            return _error_redirect("missing_sub")
        if not email or not claims.get("email_verified"):
            return _error_redirect("email_not_verified")

        # Construct the user_object for the login Request:
        #   * load the live session record by session_guid -> hydrate UserObject
        #   * set OAuthIdentities[0] to the newly-asserted identity
        # The auth tool's login branch does the Users.users register/merge
        # and mirrors the result back into Users.sessions.
        from authentication.authorizations_and_authentications_tool import (
            TOOL as AUTHN_TOOL,
            get_mongo_frontend,
            Request as AuthnRequest,
            SESSION_DB,
            SESSION_COLLECTION,
        )
        from authentication.user_object import UserObject, OAuthIdentity
        from authentication.agent_deps import AuthnDeps

        mongo = get_mongo_frontend()
        sessions_coll = mongo[SESSION_DB][SESSION_COLLECTION]
        session_doc = sessions_coll.find_one({"_id": session_guid})
        if not session_doc:
            log.error(
                "OAUTH-CALLBACK no session record for guid=%s; OAuth arrived "
                "without prior /gate visit", session_guid[:8],
            )
            return _error_redirect("no_session_record")
        try:
            session_user_object = UserObject.model_validate(
                {k: v for k, v in session_doc.items() if k != "_id"}
            )
        except Exception as exc:
            log.exception("OAUTH-CALLBACK could not validate session UserObject: %s", exc, exc=ChatHealthyException(
                                                                                            mode="oauth_session_user_object_invalid",
                                                                                            message=f"OAUTH-CALLBACK could not validate session UserObject: {exc}",
                                                                                            component="GoogleOAuthEndpoint",
                                                                                            exception=exc,
                                                                                        ), if_not_debug_log=True)
            return _error_redirect(f"session_invalid_{type(exc).__name__}")

        session_user_object.OAuthIdentities = [
            OAuthIdentity(
                identity_provider="Google",
                identity_provider_user_id=google_sub,
                email=email,
            )
        ]

        log.info(
            "OAUTH-CALLBACK handing off to AUTHN_TOOL.run(intent='login') "
            "email=%s google_sub_prefix=%s session_guid=%s",
            email, google_sub[:8], session_guid[:8] + "...",
        )
        try:
            import asyncio
            authn_deps = AuthnDeps(
                prior_guid=session_guid,
                server_env=server_env,
                mongo_frontend=mongo,
            )
            authn_resp = asyncio.run(
                AUTHN_TOOL.run(
                    authn_deps,
                    AuthnRequest(intent="login", user_object=session_user_object),
                )
            )
        except Exception as exc:
            log.exception("OAUTH-CALLBACK AUTHN_TOOL.run(login) raised: %s", exc, exc=ChatHealthyException(
                                                                                   mode="oauth_authn_login_raised",
                                                                                   message=f"OAUTH-CALLBACK AUTHN_TOOL.run(login) raised: {exc}",
                                                                                   component="GoogleOAuthEndpoint",
                                                                                   exception=exc,
                                                                               ), if_not_debug_log=True)
            return _error_redirect(f"register_failed_{type(exc).__name__}")

        user_id = authn_resp.user_object.user_id
        if not user_id:
            log.error("OAUTH-CALLBACK login returned user_object with no user_id")
            return _error_redirect("login_no_user_id")

        log.info(
            "OAUTH-CALLBACK login OK user_id=%s; setting "
            "ChatHealthyRegisteredUserCookie and redirecting to wrapper",
            user_id,
        )
        redirect = RedirectResponse(
            f"{wrapper}/?auth_email={email}",
            status_code=302,
        )
        set_chathealthy_user_cookie(redirect, user_id)
        return redirect
