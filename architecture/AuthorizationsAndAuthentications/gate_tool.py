# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""GateTool — the one universal entrance for SharedServices.

Single tool. Single entrance method (`execute`). Single nullable pydantic
input (`GateRequest`). Every operation flows through here:

  GateTool.execute(GateRequest | None)
    -> resolve session from prior_guid (cookie GUID)
    -> restamp nonce on existing session OR mint a fresh one
    -> upsert admin.sessions (keyed by GUID; doc body = UserObject JSON)
    -> validate op-specific payload against the chosen Tool's request_model
    -> Tool.execute(session_token, user_object, request)
    -> $push any declared history entry
    -> return GateResponse

Mongo work is opaque to callers but happens inside this tool. The tool
will be enriched with more responsibilities (OAuth, profile mutations,
filter capture, etc.) — that growth all happens inside execute() or its
private helpers, never leaking out to the route layer.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import MongoClient

from chathealthy_frontend_lib.authentication.session_token import SessionToken
from authentication.mintable_auth_token import MintableAuthToken
from authentication.tool_framework import (
    GateRequest,
    GateResponse,
    HistoryAppend,
    Tool,
    ToolRequest,
    ToolResponse,
)
from authentication.user_object import (
    SessionConversationHistory,
    UserObject,
)

_log = logging.getLogger("shared_services.gate_tool")

_ORIGIN = "SharedServices"
_SESSION_TTL_SECONDS = 300
# Front-end cluster, admin db, sessions collection (Skip-mandated 2026-05-14:
# canonical name is lowercase plural). Every live session lives here.
_SESSION_DB = "admin"
_SESSION_COLLECTION = "sessions"


class BootRequest(ToolRequest):
    """Boot takes no payload — it's the entrance with no operation."""


class BootResponse(ToolResponse):
    pass


class BootTool(Tool):
    """The implicit tool for `op=boot` (or omitted op). Identity-only —
    no history append. Page-load entrance is plumbing, not a turn."""
    tool_name: str = "boot"
    request_model: type = BootRequest
    response_model: type = BootResponse

    def execute(self, session_token, user_object, request):
        return BootResponse(result={"op": "boot"})


class GateTool:
    """The one universal-entrance tool. One method (`execute`), one
    nullable pydantic input (`GateRequest`)."""

    _tools: dict[str, Tool] = {}
    _client: MongoClient | None = None
    _indexes_ensured: bool = False

    @classmethod
    def register_tool(cls, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise TypeError(
                f"register_tool requires a Tool instance; got {type(tool).__name__}"
            )
        cls._tools[tool.tool_name] = tool

    @classmethod
    def execute(cls, request: GateRequest | None) -> dict[str, Any]:
        """The single entrance. `request` may be None — a first-time
        arrival with no cookie and no body is a legitimate call."""
        gate_req = request if request is not None else GateRequest()
        op = gate_req.op or "boot"
        prior_guid = gate_req.prior_guid
        payload_in = gate_req.payload or {}
        server_env = gate_req.server_env

        user_object, is_fresh_mint = cls._resolve_session(prior_guid, server_env)
        cls._persist(user_object, fresh_mint=is_fresh_mint)
        guid = user_object.current_session_token.get_auth_token()

        tool = cls._tools.get(op)
        if tool is None:
            return GateResponse(
                guid=guid,
                user_object=cls._user_object_wire(user_object),
                result={"op": op, "error": f"unknown op {op!r}"},
            ).model_dump()

        try:
            tool_request = tool.request_model.model_validate(payload_in)
        except Exception as exc:
            return GateResponse(
                guid=guid,
                user_object=cls._user_object_wire(user_object),
                result={"op": op, "error": f"payload validation failed: {exc}"},
            ).model_dump()

        tool_response = tool.execute(
            user_object.current_session_token, user_object, tool_request,
        )
        if not isinstance(tool_response, tool.response_model):
            raise RuntimeError(
                f"Tool {tool.tool_name!r} returned {type(tool_response).__name__!r}; "
                f"expected {tool.response_model.__name__!r}"
            )

        if tool_response.history_append is not None:
            cls._append_history(guid, tool_response.history_append)
            user_object = cls._reload(guid) or user_object

        return GateResponse(
            guid=guid,
            user_object=cls._user_object_wire(user_object),
            result=tool_response.result,
        ).model_dump()

    @staticmethod
    def _user_object_wire(user_object: UserObject) -> dict[str, Any]:
        """Wire serialization — no ceremonial nulls; Python-native
        datetimes (FastAPI ISO-formats them on the way out)."""
        return user_object.model_dump(mode="python", exclude_none=True)

    @classmethod
    def _append_history(cls, guid: str, directive: HistoryAppend) -> None:
        coll = cls._mongo()[_SESSION_DB][_SESSION_COLLECTION]
        coll.update_one(
            {"_id": guid},
            {"$push": {f"session_conversation_history.{directive.array}": directive.entry}},
        )

    @classmethod
    def _reload(cls, guid: str) -> UserObject | None:
        doc = cls._mongo()[_SESSION_DB][_SESSION_COLLECTION].find_one({"_id": guid})
        if not doc:
            return None
        try:
            return UserObject.model_validate({k: v for k, v in doc.items() if k != "_id"})
        except Exception as exc:
            _log.warning("could not reconstitute UserObject for %s: %s", guid[:8], exc)
            return None

    @classmethod
    def _resolve_session(
        cls, prior_guid: str | None, server_env: str,
    ) -> tuple[UserObject, bool]:
        """Resume the existing session if its GUID is alive in
        admin.sessions and the stored UserObject's `expires_at` is in
        the future; otherwise mint a fresh session.

        Returns (user_object, is_fresh_mint). is_fresh_mint=True when
        we built a brand-new session (cookie absent, GUID unknown, or
        the stored session expired).

        Flow:
          1. If `prior_guid` is set, find the doc by `_id`.
          2. Reconstitute UserObject from the doc.
          3. If user_object.expires_at > now, restamp current_session_token
             (new nonce), bump expires_at = now + 300s, return.
          4. Otherwise mint fresh.

        The 300s ceiling is enforced here in software regardless of
        Mongo's TTL purge state (the TTL index is eventually-consistent).
        """
        now = datetime.now(timezone.utc)
        if prior_guid:
            existing = cls._reload(prior_guid)
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
        session_token = cls._auth_token_to_session_token(minted)
        user_object = UserObject(
            current_session_token=session_token,
            expires_at=now + timedelta(seconds=_SESSION_TTL_SECONDS),
        )
        return user_object, True

    @staticmethod
    def _auth_token_to_session_token(auth_token: Any) -> SessionToken:
        """MintableAuthToken.manufacture returns an AuthToken; the
        SessionToken is exposed via AuthToken.to_wire()."""
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

    @classmethod
    def _mongo(cls) -> MongoClient:
        if cls._client is None:
            conn = os.getenv("MONGO_FRONTEND_connectionString")
            if not conn:
                raise RuntimeError(
                    "MONGO_FRONTEND_connectionString not set; gate cannot persist"
                )
            cls._client = MongoClient(conn, serverSelectionTimeoutMS=10000)
        return cls._client

    @classmethod
    def _ensure_indexes(cls, coll) -> None:
        if cls._indexes_ensured:
            return
        coll.create_index("expires_at", expireAfterSeconds=0, name="session_ttl_idx")
        cls._indexes_ensured = True

    @classmethod
    def _persist(cls, user_object: UserObject, fresh_mint: bool) -> None:
        """Upsert admin.sessions for this session's GUID.

        Doc shape (Skip B1, 2026-05-15): `{_id: GUID, <UserObject fields
        at top level>}`. Flat — the document body IS the User Object
        serialized as JSON (Pydantic mode='python', exclude_none=True).

        On every call:
          * $set: current_session_token (with fresh nonce) + expires_at
            (new 300s window) + session_conversation_history (carries the
            current state of the arrays; ops $push into them separately).
          * On insert only: set the WHOLE UserObject including any
            already-populated fields like ip_address.

        Identity fields written by other ops (registered_profile, etc.)
        are preserved by NOT touching them in $set.
        """
        coll = cls._mongo()[_SESSION_DB][_SESSION_COLLECTION]
        cls._ensure_indexes(coll)
        guid = user_object.current_session_token.get_auth_token()
        body = user_object.model_dump(mode="python", exclude_none=True)
        if fresh_mint:
            coll.replace_one(
                {"_id": guid},
                {"_id": guid, **body},
                upsert=True,
            )
            return
        # Resume: only refresh volatile fields (token, expires_at, and
        # whatever session_conversation_history is currently carrying —
        # ops $push their own entries; this overwrites with the
        # reconstituted in-memory copy, which is faithful to disk because
        # we just loaded it).
        volatile = {
            "current_session_token": body["current_session_token"],
            "expires_at": body["expires_at"],
            "session_conversation_history": body.get(
                "session_conversation_history",
                {"unanswered_questions": [], "ux_events": [], "utterances": []},
            ),
        }
        coll.update_one({"_id": guid}, {"$set": volatile})
