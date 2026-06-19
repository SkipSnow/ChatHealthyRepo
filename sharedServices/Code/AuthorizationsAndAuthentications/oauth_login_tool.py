# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""OAuth Login Tool — EPIC-002-F-003-S-004.

ChatHealthyTool that owns the full OAuth login flow: validate state,
exchange code for tokens (or short-circuit on local for the canned test
identities), verify id_token, gate on the hardcoded pre-alpha allow-list,
write Users.users + Users.sessions, build the user-facing welcome/fail
message.

Architectural notes:
- Single async run() method as required by ChatHealthyTool base.
- Pydantic Request + Response.
- env_check is `deps.server_env == "local"` AND `request.fake_email_override`
  in the canned-fixture set; ANY OTHER email on local falls through to the
  real Google call (proving the dev path executes on local under Test 4).
- Allow-list is 100% hardcoded per REQ-B-020.
"""
from __future__ import annotations

import os
import time
import secrets as _secrets
from datetime import datetime, timezone
from typing import Literal, Optional
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, Field
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from chathealthy_frontend_lib import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException

from authentication.agent_deps import AgentDeps
from authentication.chathealthy_tool import ChatHealthyTool
from authentication.google_oauth_endpoint import (
    GOOGLE_AUTHZ_URL,
    build_state,
    client_id as _google_client_id,
    redirect_uri as _google_redirect_uri,
)

log = ChatHealthyLoggingService()


# ──────────────────────────────────────────────────────────────────────
# Hardcoded pre-alpha allow-list — REQ-B-020.
# Operator edits this list + redeploys to authorize a new pre-alpha user.
# Case-insensitive comparison.
# ──────────────────────────────────────────────────────────────────────
PRE_ALPHA_ALLOW_LIST = frozenset(
    e.lower() for e in (
        "Claude@anthropic.ai",
    )
)


# ──────────────────────────────────────────────────────────────────────
# Canned local-fixture identities — REQ-B-013 success + REQ-B-016 fail.
# These two emails are the ONLY values that trigger the local short-circuit.
# ──────────────────────────────────────────────────────────────────────
FAKE_CODE_PREFIX = "fake_local_"


# ──────────────────────────────────────────────────────────────────────
# User-facing messages — REQ-B-013, B-016, B-017.
# ──────────────────────────────────────────────────────────────────────
def message_new_user_success(email: str) -> str:
    """REQ-B-013."""
    return (
        f"You're logged in, {email}. ChatHealthy is glad to have you as a "
        "pre-alpha user. Welcome aboard."
    )


def message_returning_user_success(email: str, last_login_iso: str) -> str:
    """REQ-B-017."""
    return (
        f"Welcome back, {email}. You last logged in at {last_login_iso}."
    )


def message_login_failed(email: Optional[str]) -> str:
    """REQ-B-016."""
    who = f" for {email}" if email else ""
    return (
        f"Login failed{who}. In this pre-alpha state Google demands that "
        "ChatHealthy registers them as pre-alpha users. Please email "
        "skip@chathealthy.ai, or text Skip at 01 (646) 408-8999 to be "
        "added to the pre-alpha list."
    )


# ──────────────────────────────────────────────────────────────────────
# Canonical Google id_token claims fixture builder (used by local fake).
# Field shape MUST match what Google's real id_token verification returns.
# Refined per Google OIDC docs (research agent confirms the field set).
# ──────────────────────────────────────────────────────────────────────
def build_fake_google_claims(email: str, audience: str) -> dict:
    now = int(time.time())
    # Synthesize a stable 21-character numeric `sub` from the email so the
    # same fake email always yields the same Google account id across runs.
    sub_int = abs(hash("google-sub-fixture::" + email.lower())) % (10**21)
    sub = str(sub_int).zfill(21)
    return {
        "iss": "https://accounts.google.com",
        "azp": audience,
        "aud": audience,
        "sub": sub,
        "email": email,
        "email_verified": True,
        "at_hash": _secrets.token_urlsafe(11)[:11],
        "name": email.split("@", 1)[0],
        "picture": "https://lh3.googleusercontent.com/a/default-user=s96-c",
        "given_name": email.split("@", 1)[0],
        "family_name": "",
        "locale": "en",
        "iat": now,
        "exp": now + 3600,
    }


# ──────────────────────────────────────────────────────────────────────
# Pydantic Request / Response — REQ for proper-tool framework conformance.
# Single Request shape covers both phases (start, callback) per
# EPIC-008-F-010-S-001-REQ-B-001 (single graph orchestrator entry).
# ──────────────────────────────────────────────────────────────────────
class Request(BaseModel):
    model_config = {"extra": "ignore"}
    phase: Literal["start", "callback"]
    identity_provider: str = "Google"
    server_env: str = Field(min_length=1)
    session_guid: str = Field(min_length=32, max_length=32)
    oauth_code: Optional[str] = None
    oauth_state: Optional[str] = None
    flow: str = "login"


class Response(BaseModel):
    outcome: Literal["redirect", "success", "fail"]
    authz_url: Optional[str] = None
    user_id: Optional[str] = None
    public_username: Optional[str] = None
    last_login_at: Optional[str] = None
    user_facing_message: str = ""
    google_claims: Optional[dict] = None
    fail_reason: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────
# Internal helpers — kept free of `log.` calls so the helper bodies can
# raise ChatHealthyException without violating Rule-005 statement 3.
# ──────────────────────────────────────────────────────────────────────
def _client_id() -> str:
    cid = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    if not cid:
        raise ChatHealthyException(
            mode="oauth_login_missing_google_client_id",
            message="GOOGLE_OAUTH_CLIENT_ID not configured on this Space.",
            component="OAuthLoginTool",
        )
    return cid


def _client_secret() -> str:
    sec = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not sec:
        raise ChatHealthyException(
            mode="oauth_login_missing_google_client_secret",
            message="GOOGLE_OAUTH_CLIENT_SECRET not configured on this Space.",
            component="OAuthLoginTool",
        )
    return sec


def _env_to_redirect_uri(server_env: str) -> str:
    table = {
        "dev":   "https://skipsnow-dev-sharedservicesspace.hf.space/auth/google/callback",
        "qa":    "https://skipsnow-qa-sharedservicesspace.hf.space/auth/google/callback",
        "prod":  "https://skipsnow-sharedservicesspace.hf.space/auth/google/callback",
        "local": "https://localhost:8002/auth/google/callback",
    }
    uri = table.get(server_env)
    if not uri:
        raise ChatHealthyException(
            mode="oauth_login_unknown_env",
            message=f"OAuthLoginTool: unknown server_env {server_env!r}.",
            component="OAuthLoginTool",
        )
    return uri


def _is_allowed_prealpha(email: str) -> bool:
    return email.lower() in PRE_ALPHA_ALLOW_LIST


def _decode_fake_code(oauth_code: str) -> Optional[str]:
    """If oauth_code is a synthetic fake code minted by the local
    /fake_google/submit flow, return the email it carries. Otherwise None."""
    if not oauth_code or not oauth_code.startswith(FAKE_CODE_PREFIX):
        return None
    import base64
    payload = oauth_code[len(FAKE_CODE_PREFIX):]
    try:
        pad = "=" * (-len(payload) % 4)
        return base64.urlsafe_b64decode(payload + pad).decode("utf-8")
    except Exception:
        return None


async def _real_google_exchange_and_verify(
    code: str, server_env: str,
) -> dict:
    """Exchange the auth code for tokens, verify the id_token, return
    the validated claims dict. Raises ChatHealthyException on any failure.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code":          code,
                "client_id":     _client_id(),
                "client_secret": _client_secret(),
                "redirect_uri":  _env_to_redirect_uri(server_env),
                "grant_type":    "authorization_code",
            },
        )
    if r.status_code != 200:
        raise ChatHealthyException(
            mode="oauth_login_token_exchange_failed",
            message=(
                f"OAuthLoginTool: Google token exchange returned HTTP "
                f"{r.status_code}: {r.text[:300]}"
            ),
            component="OAuthLoginTool",
        )
    tokens = r.json()
    id_token_str = tokens.get("id_token")
    if not id_token_str:
        raise ChatHealthyException(
            mode="oauth_login_missing_id_token",
            message="OAuthLoginTool: Google token response had no id_token.",
            component="OAuthLoginTool",
        )
    # Synchronous Google library; safe to call inside the async function.
    claims = google_id_token.verify_oauth2_token(
        id_token_str,
        google_requests.Request(),
        audience=_client_id(),
    )
    return claims


def _persist_login(
    sessions_coll, users_coll, claims: dict, session_guid: str,
) -> tuple[str, Optional[str]]:
    from authentication.user_object import UserObject, OAuthIdentity
    email = claims["email"]
    sub = claims["sub"]
    identity_provider = "Google"
    now_iso = datetime.now(timezone.utc).isoformat()

    guest_doc = sessions_coll.find_one({"_id": session_guid}) or {}
    guest_payload = {k: v for k, v in guest_doc.items() if k != "_id"}
    if not guest_payload:
        raise ChatHealthyException(
            mode="oauth_login_no_session_for_callback",
            message=f"OAuthLoginTool: no session_doc for _id={session_guid[:8]}...",
            component="OAuthLoginTool",
        )
    guest = UserObject.model_validate(guest_payload)

    existing = users_coll.find_one({
        "user_object.OAuthIdentities": {
            "$elemMatch": {
                "identity_provider": identity_provider,
                "identity_provider_user_id": sub,
            },
        },
    })

    if existing is None:
        user_id = "u-" + _secrets.token_urlsafe(16)
        stored = UserObject(
            current_session_token="NULL",
            expires_at=guest.expires_at,
            is_registered=True,
            user_id=user_id,
            user_type="Prospect",
            public_username=email,
            OAuthIdentities=[OAuthIdentity(
                identity_provider=identity_provider,
                identity_provider_user_id=sub,
                email=email,
            )],
        )
        prior_last_login = None
        first_login_at = now_iso
    else:
        user_id = existing["user_id"]
        prior_last_login = existing.get("last_login_at")
        first_login_at = existing.get("first_login_at", now_iso)
        stored = UserObject.model_validate(existing.get("user_object", {}))
        for ident in stored.OAuthIdentities:
            if (ident.identity_provider == identity_provider
                    and ident.identity_provider_user_id == sub):
                ident.email = email

    merged = stored.merge(guest)
    merged_dump = merged.model_dump(mode="json", exclude_none=True)

    users_result = users_coll.update_one(
        {"user_id": user_id},
        {"$set": {
            "user_id": user_id,
            "user_object": merged_dump,
            "first_login_at": first_login_at,
            "last_login_at": now_iso,
        }},
        upsert=True,
    )
    if not (users_result.matched_count == 1 or users_result.upserted_id is not None):
        raise ChatHealthyException(
            mode="oauth_login_users_doc_write_failed",
            message=f"OAuthLoginTool: Users.users write failed for user_id={user_id}.",
            component="OAuthLoginTool",
        )

    sessions_result = sessions_coll.replace_one(
        {"_id": session_guid},
        {"_id": session_guid, **merged_dump},
        upsert=True,
    )
    if not (sessions_result.matched_count == 1 or sessions_result.upserted_id is not None):
        raise ChatHealthyException(
            mode="oauth_login_session_doc_write_failed",
            message=(
                f"OAuthLoginTool: session_doc write failed for _id="
                f"{session_guid[:8]}..."
            ),
            component="OAuthLoginTool",
        )
    return user_id, prior_last_login


def _build_authz_url(server_env: str, session_guid: str, flow: str) -> str:
    state = build_state(server_env, session_guid=session_guid)
    if server_env == "local":
        return f"https://localhost:8002/fake_google/auth?state={state}&flow={flow}"
    params = {
        "client_id":     _google_client_id(),
        "redirect_uri":  _google_redirect_uri(server_env),
        "response_type": "code",
        "scope":         "openid email",
        "state":         state,
        "prompt":        "select_account" if flow == "register" else "none",
        "access_type":   "online",
    }
    return GOOGLE_AUTHZ_URL + "?" + urlencode(params)


class OAuthLoginTool(ChatHealthyTool):
    """EPIC-002-F-003-S-004 Login & Registration via OAuth."""
    TOOL_NAME = "oauth_login"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        session_prefix = (request.session_guid or "")[:8] + "..."
        log.info(
            "OAUTH-AUDIT attempt phase=%s identity_provider=%s server_env=%s "
            "session_guid_prefix=%s flow=%s has_oauth_code=%s has_oauth_state=%s",
            request.phase, request.identity_provider, deps.server_env,
            session_prefix, request.flow,
            bool(request.oauth_code), bool(request.oauth_state),
        )

        if request.phase == "start":
            response = self._run_start(deps, request)
        else:
            response = await self._run_callback(deps, request)

        log.info(
            "OAUTH-AUDIT result phase=%s outcome=%s public_username=%s "
            "user_id=%s fail_reason=%s session_guid_prefix=%s "
            "authz_url_built=%s",
            request.phase, response.outcome, response.public_username,
            response.user_id, response.fail_reason, session_prefix,
            bool(response.authz_url),
        )
        return response

    def _run_start(self, deps: AgentDeps, request: "Request") -> "Response":
        authz_url = _build_authz_url(
            deps.server_env, request.session_guid, request.flow,
        )
        return Response(outcome="redirect", authz_url=authz_url)

    async def _run_callback(
        self, deps: AgentDeps, request: "Request",
    ) -> "Response":
        if not request.oauth_code:
            return Response(
                outcome="fail",
                user_facing_message=message_login_failed(None),
                fail_reason="missing_oauth_code",
            )
        fake_email = _decode_fake_code(request.oauth_code)
        if fake_email is not None and deps.server_env == "local":
            audience = os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "local-fake-audience"
            claims = build_fake_google_claims(fake_email, audience)
        else:
            claims = await _real_google_exchange_and_verify(
                request.oauth_code, deps.server_env,
            )

        email = claims.get("email", "")
        if not email or not claims.get("email_verified"):
            return Response(
                outcome="fail",
                user_facing_message=message_login_failed(email or None),
                fail_reason="email_not_verified",
                google_claims=claims,
            )

        if not _is_allowed_prealpha(email):
            return Response(
                outcome="fail",
                user_facing_message=message_login_failed(email),
                fail_reason="not_on_prealpha_allow_list",
                google_claims=claims,
            )

        sessions_coll = deps.mongo_frontend["Users"]["sessions"]
        users_coll = deps.mongo_frontend["Users"]["users"]
        user_id, prior_last_login = _persist_login(
            sessions_coll, users_coll, claims, request.session_guid,
        )

        if prior_last_login is None:
            user_facing = message_new_user_success(email)
        else:
            user_facing = message_returning_user_success(email, prior_last_login)

        return Response(
            outcome="success",
            user_id=user_id,
            public_username=email,
            last_login_at=prior_last_login,
            user_facing_message=user_facing,
            google_claims=claims,
        )


TOOL = OAuthLoginTool()
