# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""AuthorizationsAndAuthentications tool — the auth-only Agent.

Runs first on every /gate hit. Its scope is exactly identification +
authorization:

  * Read the prior_guid (cookie).
  * If found AND not expired: reload UserObject from admin.sessions,
    restamp current_session_token's nonce, push expires_at forward 300s.
  * Otherwise: mint a fresh guest SessionToken + UserObject.
  * Persist to admin.sessions (upsert).
  * Return the user_object + a fresh_mint flag.

It does NOT dispatch ops. Its successor — UniversalNavigation — receives
the user_object on AgentDeps and decides where the user flows.

Today this is deterministic Python (no LLM judgment needed). The module
exposes the canonical *_tool.py contract: TOOL_NAME, Request, Response,
run() — uniform with the LLM-backed tools elsewhere in the codebase.
"""
from __future__ import annotations

import logging
import os
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

_indexes_ensured: bool = False


class Request(BaseModel):
    """No payload — AuthN runs identically on every gate hit."""
    model_config = {"extra": "ignore"}


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


def _resolve_session(
    coll, prior_guid: Optional[str], server_env: str,
) -> tuple[UserObject, bool]:
    """Resume an existing session (restamp nonce) or mint a fresh guest.

    Returns (user_object, fresh_mint). fresh_mint=True when we built a
    brand-new session because prior_guid was absent / unknown / expired.
    """
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


def _persist(coll, user_object: UserObject, fresh_mint: bool) -> None:
    """Upsert admin.sessions for this session's GUID.

    Doc shape: `{_id: GUID, <UserObject fields at top level>}`.

    Fresh mint: replace the whole doc.
    Resume: $set only volatile fields (token + expires_at + history arrays).
            Identity fields populated by other ops are preserved by NOT
            touching them.
    """
    _ensure_indexes(coll)
    guid = user_object.current_session_token.get_auth_token()
    body = user_object.model_dump(mode="python", exclude_none=True)
    if fresh_mint:
        coll.replace_one(
            {"_id": guid},
            {"_id": guid, **body},
            upsert=True,
        )
        return
    volatile = {
        "current_session_token": body["current_session_token"],
        "expires_at": body["expires_at"],
        "session_conversation_history": body.get(
            "session_conversation_history",
            {"unanswered_questions": [], "ux_events": [], "utterances": []},
        ),
    }
    coll.update_one({"_id": guid}, {"$set": volatile})


class AuthorizationsAndAuthenticationsTool(ChatHealthyTool):
    """The bootstrap tool. Same class owns both ends of the session:

      * `run()`     — resolves or mints the user_object (read/mint).
      * `persist()` — writes the (possibly-mutated) user_object back to
                      admin.sessions (sole writer).

    Tools downstream of AuthN mutate `deps.user_object` in memory only;
    the gate route calls `AUTHN_TOOL.persist(...)` once at the very end
    of the request so all mutations land atomically.
    """
    TOOL_NAME = "authn"
    Request = Request
    Response = Response

    async def run(self, deps: AuthnDeps, request: Optional["Request"] = None) -> "Response":
        coll = deps.mongo_frontend[_SESSION_DB][_SESSION_COLLECTION]
        user_object, fresh_mint = _resolve_session(coll, deps.prior_guid, deps.server_env)
        return self.Response(user_object=user_object, fresh_mint=fresh_mint)

    async def persist(self, deps: AuthnDeps, user_object, fresh_mint: bool) -> None:
        """Single-writer Mongo persistence. Called by the gate route AFTER
        every downstream tool has completed mutating the user_object."""
        coll = deps.mongo_frontend[_SESSION_DB][_SESSION_COLLECTION]
        _persist(coll, user_object, fresh_mint)


TOOL = AuthorizationsAndAuthenticationsTool()


# Module-level Mongo client cache (one connection per process).
_mongo_client = None


def get_mongo_frontend():
    """Lazy singleton for the front-end cluster client (admin.sessions)."""
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
