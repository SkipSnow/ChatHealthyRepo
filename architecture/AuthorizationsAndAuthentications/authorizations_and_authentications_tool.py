# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""AuthorizationsAndAuthentications tool — the auth-only Agent.

Runs first on every /gate hit. Two top-level routing paths:

  * utterance path  - default; resolve or mint the UserObject, return so
                      UniversalNavigation can dispatch downstream.
  * control path    - the request identifies a control use case. Today
                      one control use case is wired: login_register,
                      which returns a Response carrying redirect_url to
                      the SharedServices /auth/google/start endpoint.

It exposes the canonical *_tool.py contract (TOOL_NAME, Request,
Response, run()) plus a persist() method that is the sole writer to
both admin.sessions (per-session) AND admin.users (long-lived mirror)
when a users record exists.

After Google OAuth completes, the OAuth callback route calls
TOOL.handle_oauth_login(...) to register-or-find the users record,
which returns the user_id that the callback sets in
ChatHealthyUserCookie.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel

from authentication.agent_deps import AuthnDeps
from authentication.chathealthy_tool import ChatHealthyTool
from authentication.mintable_auth_token import MintableAuthToken
from authentication.user_object import UserObject
from chathealthy_frontend_lib.authentication.session_token import SessionToken

_log = logging.getLogger("shared_services.authn")

_ORIGIN = "SharedServices"
_SESSION_TTL_SECONDS = 300
_SESSION_DB = "admin"
_SESSION_COLLECTION = "sessions"
_USERS_COLLECTION = "users"

# SharedServices Space URL per env — used to build the absolute
# redirect_url returned for login_register.
_ENV_TO_SHARED_URL = {
    "dev":   "https://skipsnow-dev-sharedservicesspace.hf.space",
    "qa":    "https://skipsnow-qa-sharedservicesspace.hf.space",
    "prod":  "https://skipsnow-sharedservicesspace.hf.space",
    "local": "https://localhost:8002",
}

_INTENT_UTTERANCE = "utterance"
_INTENT_LOGIN_REGISTER = "login_register"
_KNOWN_INTENTS = frozenset({_INTENT_UTTERANCE, _INTENT_LOGIN_REGISTER})

_indexes_ensured: bool = False


class Request(BaseModel):
    """Optional intent. Absent or "utterance" -> utterance path. Any other
    recognized value identifies a control use case; unknown intents are a
    hard error per S-004-REQ-T-003."""
    intent: Optional[str] = None
    model_config = {"extra": "ignore"}


class Response(BaseModel):
    user_object: UserObject
    fresh_mint: bool
    redirect_url: Optional[str] = None


def _auth_token_to_session_token(auth_token: Any) -> SessionToken:
    if isinstance(auth_token, SessionToken):
        return auth_token
    to_wire = getattr(auth_token, "to_wire", None)
    if callable(to_wire):
        wired = to_wire()
        if isinstance(wired, SessionToken):
            return wired
    raise RuntimeError(
        f"MintableAuthToken.manufacture returned {type(auth_token).__name__!r}; "
        "no SessionToken accessible via to_wire()"
    )


def _ensure_indexes(coll) -> None:
    global _indexes_ensured
    if _indexes_ensured:
        return
    coll.create_index("expires_at", expireAfterSeconds=0, name="session_ttl_idx")
    _indexes_ensured = True


def _reload(coll, guid: str) -> Optional[UserObject]:
    doc = coll.find_one({"_id": guid})
    if not doc:
        return None
    try:
        return UserObject.model_validate({k: v for k, v in doc.items() if k != "_id"})
    except Exception as exc:
        _log.warning("could not reconstitute UserObject for %s: %s", guid[:8], exc)
        return None


def _resolve_session(
    coll, prior_guid: Optional[str], server_env: str,
) -> tuple[UserObject, bool]:
    """Resume an existing session (restamp nonce) or mint a fresh guest."""
    now = datetime.now(timezone.utc)
    if prior_guid:
        existing = _reload(coll, prior_guid)
        if existing is not None:
            expires_at = existing.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at > now:
                try:
                    existing.current_session_token.put_nonce(_ORIGIN)
                    existing.expires_at = now + timedelta(seconds=_SESSION_TTL_SECONDS)
                    return existing, False
                except Exception as exc:
                    _log.warning(
                        "restamp of stored session %s failed (%s: %s); minting fresh",
                        prior_guid[:8], type(exc).__name__, exc,
                    )

    minted = MintableAuthToken.manufacture(server_env=server_env)
    session_token = _auth_token_to_session_token(minted)
    user_object = UserObject(
        current_session_token=session_token,
        expires_at=now + timedelta(seconds=_SESSION_TTL_SECONDS),
    )
    return user_object, True


def _user_id_for_guid(users_coll, guid: str) -> Optional[str]:
    """Return user_id whose stored user_object holds this session's GUID."""
    doc = users_coll.find_one(
        {"user_object.current_session_token.auth_token": guid},
        {"user_id": 1},
    )
    return doc["user_id"] if doc else None


def _user_id_for_provider_sub(
    users_coll, provider: str, provider_user_id: str,
) -> Optional[str]:
    """Look up a users record by an entry in user_object.OAuthIdentities."""
    doc = users_coll.find_one(
        {"user_object.OAuthIdentities": {
            "$elemMatch": {
                "provider": provider,
                "provider_user_id": provider_user_id,
            },
        }},
        {"user_id": 1},
    )
    return doc["user_id"] if doc else None


def _persist(coll, users_coll, user_object: UserObject, fresh_mint: bool) -> None:
    """Sole writer for admin.sessions (per-session, every persist) and
    admin.users (mirror, only when user_object.is_registered == True)."""
    _ensure_indexes(coll)
    guid = user_object.current_session_token.get_auth_token()
    body = user_object.model_dump(mode="python", exclude_none=True)
    if fresh_mint:
        coll.replace_one(
            {"_id": guid},
            {"_id": guid, **body},
            upsert=True,
        )
    else:
        coll.replace_one(
            {"_id": guid},
            {"_id": guid, **body},
            upsert=True,
        )

    # Mirror to users collection when is_registered == True (REQ-T-010).
    if user_object.is_registered is True:
        user_id = _user_id_for_guid(users_coll, guid)
        if user_id:
            users_coll.update_one(
                {"user_id": user_id},
                {"$set": {"user_object": body}},
            )


class AuthorizationsAndAuthenticationsTool(ChatHealthyTool):
    """Sole owner of session state and the registered-users mirror.

      * `run()`                  - resolves or mints user_object; routes
                                   utterance vs control use case.
      * `persist()`              - writes admin.sessions AND admin.users.
      * `handle_oauth_login()`   - called by the Google OAuth callback
                                   after the token exchange completes;
                                   returns the user_id that the callback
                                   sets in ChatHealthyUserCookie.
    """
    TOOL_NAME = "authn"
    Request = Request
    Response = Response

    async def run(self, deps: AuthnDeps, request: Optional["Request"] = None) -> "Response":
        coll = deps.mongo_frontend[_SESSION_DB][_SESSION_COLLECTION]
        user_object, fresh_mint = _resolve_session(coll, deps.prior_guid, deps.server_env)

        intent = (request.intent if request is not None else None) or _INTENT_UTTERANCE
        if intent not in _KNOWN_INTENTS:
            raise ValueError(
                f"AuthN: unknown intent {intent!r}; "
                f"expected one of {sorted(_KNOWN_INTENTS)}"
            )

        redirect_url: Optional[str] = None
        if intent == _INTENT_LOGIN_REGISTER:
            shared = _ENV_TO_SHARED_URL.get(deps.server_env)
            if not shared:
                raise RuntimeError(
                    f"AuthN: no SharedServices URL for env {deps.server_env!r}"
                )
            redirect_url = f"{shared}/auth/google/start"

        return self.Response(
            user_object=user_object,
            fresh_mint=fresh_mint,
            redirect_url=redirect_url,
        )

    async def persist(self, deps: AuthnDeps, user_object, fresh_mint: bool) -> None:
        coll = deps.mongo_frontend[_SESSION_DB][_SESSION_COLLECTION]
        users_coll = deps.mongo_frontend[_SESSION_DB][_USERS_COLLECTION]
        _persist(coll, users_coll, user_object, fresh_mint)

    def handle_oauth_login(
        self, mongo_frontend, *,
        google_sub: str, email: str, session_guid: str,
    ) -> str:
        """Register-or-find a users record for this Google identity, bind
        it to the current session, and return the user_id that the OAuth
        callback MUST set in ChatHealthyUserCookie.

        First-time (REQ-T-009): creates a users record with a freshly
        generated user_id and a user_object whose OAuthIdentities array
        contains one Google entry. The new user_object has is_registered
        = True, user_type = "Prospect", and public_username = Google sub.

        Returning (REQ-T-012): reuses the existing user_id; refreshes the
        Google entry's email to this callback's value; REPLACES the
        session's guest user_object with the stored one (preserving the
        current session token + expires_at so the live session stays
        intact).
        """
        provider = "Google"
        sessions_coll = mongo_frontend[_SESSION_DB][_SESSION_COLLECTION]
        users_coll = mongo_frontend[_SESSION_DB][_USERS_COLLECTION]

        session_doc = sessions_coll.find_one({"_id": session_guid})
        if not session_doc:
            raise RuntimeError(
                f"AuthN.handle_oauth_login: no session record for "
                f"{session_guid[:8]}; OAuth callback arrived without a "
                "prior /gate visit"
            )
        session_user_object = {k: v for k, v in session_doc.items() if k != "_id"}
        live_token = session_user_object.get("current_session_token")
        live_expires = session_user_object.get("expires_at")

        existing_user_id = _user_id_for_provider_sub(
            users_coll, provider=provider, provider_user_id=google_sub,
        )

        if existing_user_id is None:
            # REQ-T-009: register.
            user_id = "u-" + secrets.token_urlsafe(16)
            new_user_object = dict(session_user_object)
            new_user_object["public_username"] = google_sub
            new_user_object["user_type"] = "Prospect"
            new_user_object["is_registered"] = True
            new_user_object["OAuthIdentities"] = [{
                "provider": provider,
                "provider_user_id": google_sub,
                "email": email,
            }]
            users_coll.insert_one({
                "user_id": user_id,
                "user_object": new_user_object,
            })
            # Reflect the upgraded state into the live session record so
            # the next persist() round-trip sees a consistent UserObject.
            sessions_coll.update_one(
                {"_id": session_guid},
                {"$set": {
                    "public_username": google_sub,
                    "user_type": "Prospect",
                    "is_registered": True,
                    "OAuthIdentities": new_user_object["OAuthIdentities"],
                }},
            )
            return user_id

        # REQ-T-012 + REQ-T-015: returning. Refresh the entry email,
        # then REPLACE the live session's user_object with the stored
        # one — but first concatenate the guest's session_conversation_
        # history onto the stored history (stored first, guest second)
        # so the pre-login turns aren't lost. Keep the live session
        # token + expires_at so the 5-min TTL is unbroken.
        users_coll.update_one(
            {
                "user_id": existing_user_id,
                "user_object.OAuthIdentities": {
                    "$elemMatch": {
                        "provider": provider,
                        "provider_user_id": google_sub,
                    },
                },
            },
            {"$set": {"user_object.OAuthIdentities.$.email": email}},
        )
        existing = users_coll.find_one({"user_id": existing_user_id})
        stored_user_object = (existing or {}).get("user_object", {}) or {}
        rebound = dict(stored_user_object)
        # Merge conversation histories: stored first, guest appended.
        guest_hist = (session_user_object.get("session_conversation_history") or {})
        stored_hist = (stored_user_object.get("session_conversation_history") or {})
        merged_hist = {
            "utterances": list(stored_hist.get("utterances") or []) + list(guest_hist.get("utterances") or []),
            "ux_events": list(stored_hist.get("ux_events") or []) + list(guest_hist.get("ux_events") or []),
            "unanswered_questions": list(stored_hist.get("unanswered_questions") or []) + list(guest_hist.get("unanswered_questions") or []),
        }
        rebound["session_conversation_history"] = merged_hist
        if live_token is not None:
            rebound["current_session_token"] = live_token
        if live_expires is not None:
            rebound["expires_at"] = live_expires
        sessions_coll.replace_one(
            {"_id": session_guid},
            {"_id": session_guid, **rebound},
            upsert=True,
        )
        # Mirror the merged history back to admin.users so the appended
        # entries are durable across sessions (REQ-T-015).
        users_coll.update_one(
            {"user_id": existing_user_id},
            {"$set": {"user_object.session_conversation_history": merged_hist}},
        )
        return existing_user_id


TOOL = AuthorizationsAndAuthenticationsTool()


# Module-level Mongo client cache (one connection per process).
_mongo_client = None


def get_mongo_frontend():
    """Lazy singleton for the front-end cluster client (admin.sessions + admin.users)."""
    global _mongo_client
    if _mongo_client is None:
        from pymongo import MongoClient
        conn = os.getenv("MONGO_FRONTEND_connectionString")
        if not conn:
            raise RuntimeError(
                "MONGO_FRONTEND_connectionString not set; AuthN tool cannot persist."
            )
        _mongo_client = MongoClient(conn, serverSelectionTimeoutMS=10000)
    return _mongo_client
