# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""AuthorizationsAndAuthentications tool — pure session-data work.

The tool exposes the canonical *_tool.py contract (TOOL_NAME, Request,
Response, run()) plus a persist() method.

run() is a three-way switch on `request.intent`:

  * "manufacture_session" — mint a fresh SessionToken, stamp it onto
    the incoming user_object's current_session_token field (which
    arrived as the sentinel "NULL"), bump expires_at, return.

  * "manage_session" — the incoming user_object carries a real
    SessionToken (the gateway loaded it from Users.sessions). Restamp
    the nonce in place, bump expires_at, return.

  * "login" — the incoming user_object carries a real SessionToken
    AND an OAuthIdentities[0] entry populated by the OAuth callback.
    Register-or-merge the users record in Users.users, mirror the
    resulting user_object back into Users.sessions, return.

The tool has no knowledge of HTTP, URLs, or callers. It does not
dispatch on cookie state, request shape, or any input outside its
typed Request. The gateway decides the intent; the tool executes it.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel

from authentication.agent_deps import AuthnDeps
from authentication.chathealthy_tool import ChatHealthyTool
from authentication.mintable_auth_token import MintableAuthToken
from authentication.user_object import UserObject
from chathealthy_frontend_lib.authentication.session_token import SessionToken

_log = logging.getLogger("shared_services.authn")

_ORIGIN = "SharedServices"
_SESSION_TTL_SECONDS = 300
_SESSION_DB = "Users"
_SESSION_COLLECTION = "sessions"
_USERS_DB = "Users"
_USERS_COLLECTION = "users"

_indexes_ensured: bool = False


class Request(BaseModel):
    """The auth tool's input. The gateway picks `intent` from the three
    permitted operations and hands a `user_object` populated to the
    degree appropriate for that operation:

      * manufacture_session: user_object.current_session_token == "NULL"
        (sentinel — no session exists yet). The branch mints the token.
      * manage_session: user_object.current_session_token is a real
        SessionToken loaded from Users.sessions by the gateway.
      * login: user_object.current_session_token is a real SessionToken
        AND user_object.OAuthIdentities[0] is the newly asserted
        identity from the OAuth callback.
    """
    intent: Literal["manufacture_session", "manage_session", "login"]
    user_object: UserObject


class Response(BaseModel):
    user_object: UserObject
    fresh_mint: bool


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


def _manufacture_session(user_object: UserObject, server_env: str) -> UserObject:
    """Mint a SessionToken in place. The incoming user_object's
    current_session_token is the sentinel "NULL"; replace it with a
    real SessionToken and set expires_at to NOW + TTL."""
    now = datetime.now(timezone.utc)
    minted = MintableAuthToken.manufacture(server_env=server_env)
    user_object.current_session_token = _auth_token_to_session_token(minted)
    user_object.expires_at = now + timedelta(seconds=_SESSION_TTL_SECONDS)
    return user_object


def _manage_session(user_object: UserObject) -> UserObject:
    """Restamp the nonce on the existing SessionToken, bump expires_at.
    The incoming user_object carries a real SessionToken — the gateway
    loaded it from Users.sessions."""
    now = datetime.now(timezone.utc)
    if not isinstance(user_object.current_session_token, SessionToken):
        raise ValueError(
            "manage_session requires a real SessionToken on the incoming "
            "user_object; got sentinel/None."
        )
    user_object.current_session_token.put_nonce(_ORIGIN)
    user_object.expires_at = now + timedelta(seconds=_SESSION_TTL_SECONDS)
    return user_object


def _login(
    sessions_coll, users_coll, user_object: UserObject,
) -> UserObject:
    """Register or merge the OAuth identity carried on the incoming
    user_object's OAuthIdentities[0]. Mirror the resulting user_object
    into BOTH Users.sessions and Users.users.

    Pre-conditions (gateway must hold):
      * user_object.current_session_token is a real SessionToken.
      * user_object.OAuthIdentities[0] is populated with the newly
        asserted identity {identity_provider, identity_provider_user_id,
        email} from the OAuth callback.
    """
    if not isinstance(user_object.current_session_token, SessionToken):
        raise ValueError(
            "login requires a real SessionToken on the incoming user_object."
        )
    if not user_object.OAuthIdentities:
        raise ValueError(
            "login requires user_object.OAuthIdentities[0] populated with "
            "the newly asserted identity."
        )
    new_identity = user_object.OAuthIdentities[0]
    identity_provider = new_identity.identity_provider
    identity_provider_user_id = new_identity.identity_provider_user_id
    email = new_identity.email
    session_guid = user_object.current_session_token.get_auth_token()

    existing_user_id = _user_id_for_identity_provider_sub(
        users_coll,
        identity_provider=identity_provider,
        identity_provider_user_id=identity_provider_user_id,
    )

    if existing_user_id is None:
        # First-time: register a fresh users record. user_object becomes
        # the registered record; mirror it into Users.users.
        user_id = "u-" + secrets.token_urlsafe(16)
        user_object.public_username = email
        user_object.user_type = "Prospect"
        user_object.is_registered = True
        user_object.user_id = user_id
        body = user_object.model_dump(mode="python", exclude_none=True)
        users_coll.insert_one({"user_id": user_id, "user_object": body})
        sessions_coll.replace_one(
            {"_id": session_guid},
            {"_id": session_guid, **body},
            upsert=True,
        )
        _log.info(
            "OAUTH-LOGIN registered new user user_id=%s session_guid=%s",
            user_id, session_guid[:8] + "...",
        )
        return user_object

    # Returning: refresh email on the matching identity entry, load the
    # stored UserObject, merge stored.merge(guest), write the merged
    # record back to both collections.
    users_coll.update_one(
        {
            "user_id": existing_user_id,
            "user_object.OAuthIdentities": {
                "$elemMatch": {
                    "identity_provider": identity_provider,
                    "identity_provider_user_id": identity_provider_user_id,
                },
            },
        },
        {"$set": {"user_object.OAuthIdentities.$.email": email}},
    )
    existing = users_coll.find_one({"user_id": existing_user_id})
    stored_user_object_doc = (existing or {}).get("user_object", {}) or {}
    try:
        db_record = UserObject.model_validate(stored_user_object_doc)
    except Exception as exc:
        raise RuntimeError(
            f"AuthN.login: could not validate stored UserObject for merge "
            f"({type(exc).__name__}: {exc})"
        )
    merged = db_record.merge(user_object)
    merged_body = merged.model_dump(mode="python", exclude_none=True)
    sessions_coll.replace_one(
        {"_id": session_guid},
        {"_id": session_guid, **merged_body},
        upsert=True,
    )
    users_coll.update_one(
        {"user_id": existing_user_id},
        {"$set": {"user_object": merged_body}},
    )
    _log.info(
        "OAUTH-LOGIN returning user_id=%s merged session_guid=%s",
        existing_user_id, session_guid[:8] + "...",
    )
    return merged


def _user_id_for_guid(users_coll, guid: str) -> Optional[str]:
    """Return user_id whose stored user_object holds this session's GUID."""
    doc = users_coll.find_one(
        {"user_object.current_session_token.auth_token": guid},
        {"user_id": 1},
    )
    return doc["user_id"] if doc else None


def _user_id_for_identity_provider_sub(
    users_coll, identity_provider: str, identity_provider_user_id: str,
) -> Optional[str]:
    """Look up a users record by an entry in user_object.OAuthIdentities.

    Both the Mongo query keys AND the in-memory dict keys are the canonical
    schema names (identity_provider, identity_provider_user_id) per
    ChatHealthyUserStateSchema.json. The legacy ('provider' /
    'provider_user_id') names are no longer used.
    """
    doc = users_coll.find_one(
        {"user_object.OAuthIdentities": {
            "$elemMatch": {
                "identity_provider": identity_provider,
                "identity_provider_user_id": identity_provider_user_id,
            },
        }},
        {"user_id": 1},
    )
    return doc["user_id"] if doc else None


def _persist(coll, users_coll, user_object: UserObject, fresh_mint: bool) -> None:
    """Sole writer for Users.sessions (per-session, every persist) and
    Users.users (mirror, only when user_object.is_registered == True)."""
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
    """Pure session-data work. Three operations on `user_object`:

      * `run(Request(intent="manufacture_session", user_object=...))`
        Mint a SessionToken in place. user_object.current_session_token
        arrives as the sentinel "NULL"; leaves as a real SessionToken.

      * `run(Request(intent="manage_session", user_object=...))`
        Restamp the nonce + bump expires_at. Gateway already loaded the
        session record from Users.sessions and hydrated user_object.

      * `run(Request(intent="login", user_object=...))`
        Register-or-merge the OAuth identity carried on
        user_object.OAuthIdentities[0]. Mirror result into
        Users.sessions AND Users.users.

    `persist()` is the sole writer for the post-utterance write-back of
    user_object to Users.sessions (and to Users.users mirror when
    is_registered).
    """
    TOOL_NAME = "authn"
    Request = Request
    Response = Response

    async def run(self, deps: AuthnDeps, request: "Request") -> "Response":
        if request.intent == "manufacture_session":
            user_object = _manufacture_session(request.user_object, deps.server_env)
            return self.Response(user_object=user_object, fresh_mint=True)
        if request.intent == "manage_session":
            user_object = _manage_session(request.user_object)
            return self.Response(user_object=user_object, fresh_mint=False)
        # intent == "login"
        sessions_coll = deps.mongo_frontend[_SESSION_DB][_SESSION_COLLECTION]
        users_coll = deps.mongo_frontend[_USERS_DB][_USERS_COLLECTION]
        user_object = _login(sessions_coll, users_coll, request.user_object)
        return self.Response(user_object=user_object, fresh_mint=False)

    async def persist(self, deps: AuthnDeps, user_object, fresh_mint: bool) -> None:
        coll = deps.mongo_frontend[_SESSION_DB][_SESSION_COLLECTION]
        users_coll = deps.mongo_frontend[_USERS_DB][_USERS_COLLECTION]
        _persist(coll, users_coll, user_object, fresh_mint)


TOOL = AuthorizationsAndAuthenticationsTool()


# Module-level Mongo client cache (one connection per process).
_mongo_client = None


def get_mongo_frontend():
    """Lazy singleton for the front-end cluster client (Users.sessions + Users.users)."""
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
