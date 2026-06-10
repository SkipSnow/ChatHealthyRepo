# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""UniversalNavigation tool — the graph orchestrator.

Runs after AuthorizationsAndAuthentications has established the user
(deps.user_object). Dispatches by op to a graph node handler:

  * boot              — identity-only handshake (page load)
  * splash            — render the 4-thread SharedServices splash
  * record_ux_event   — append a UX-control event to ux_events[]
  * utterance         — capture typed text + route to specialty_filter
                        + provider_search_and_selection (streams events
                        progressively to the FE)

Every handler reads its input off deps.user_object (the working memory)
and emits its result via deps.stream(...). The dispatcher returns a
NavResult carrying the final event; the gate route persists the
mutated user_object back to Users.sessions after the run.

Canonical *_tool.py exports: TOOL_NAME, Request, Response, run().
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Literal, Optional, Union

from pydantic import BaseModel, Field

from authentication.agent_deps import (
    AgentDeps,
    AuthnDeps,
    append_action,
)
from authentication.chathealthy_tool import ChatHealthyTool
from authentication import (
    authorizations_and_authentications_tool as authn,
    provider_detail_tool,
)
from authentication.user_object import UserObject
from UtteranceManager import manager as utterance_manager

_log = logging.getLogger("shared_services.universal_navigation")

TOOL_NAME = "universal_navigation"

# ────────────────────────────────────────────────────────────────────
# Wire-level constants — gateway-level concerns that live with the
# orchestrator since /gate is now thin HTTP plumbing only.
# ────────────────────────────────────────────────────────────────────

_ENV = os.getenv("ENV_PREFIX", "dev")
_SESSION_TTL_SECONDS = 300

_WIRE_INTENT_UTTERANCE = "utterance"
_WIRE_INTENT_LOGIN_REGISTER = "login_register"
_KNOWN_WIRE_INTENTS = frozenset({_WIRE_INTENT_UTTERANCE, _WIRE_INTENT_LOGIN_REGISTER})

_ENV_TO_SHARED_URL = {
    "dev":   "https://skipsnow-dev-sharedservicesspace.hf.space",
    "qa":    "https://skipsnow-qa-sharedservicesspace.hf.space",
    "prod":  "https://skipsnow-sharedservicesspace.hf.space",
    "local": "https://localhost:8002",
}


# ────────────────────────────────────────────────────────────────────
# Request / Response contracts
# ────────────────────────────────────────────────────────────────────

class Request(BaseModel):
    """Op + opaque payload. The router picks a handler by `op`."""
    op: str = Field(default="boot")
    payload: dict[str, Any] = Field(default_factory=dict)


class Response(BaseModel):
    kind: str
    result: dict[str, Any] = Field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────
# Gate-level Request / Response — what /gate hands the orchestrator
# and what comes back. /gate only does HTTP parsing + response shaping.
# ────────────────────────────────────────────────────────────────────


@dataclass
class GateRequest:
    op: str
    payload: dict[str, Any] = field(default_factory=dict)
    intent: Optional[str] = None
    prior_guid: Optional[str] = None
    want_ndjson: bool = False


@dataclass
class GateResponse:
    """What the orchestrator returns to /gate.

    cookie_value: the 67-byte session-token bytes (`ch_session` cookie
    value). /gate puts it on the HTTP Response via `set_cookie`.

    body_kind == 'ndjson_stream' → body_data is an async iterator of
    bytes; /gate returns StreamingResponse(body_data).

    body_kind == 'ndjson_bytes' → body_data is a single bytes blob (e.g.,
    the redirect event serialized as one NDJSON line); /gate returns
    Response(content=body_data, media_type='application/x-ndjson').

    body_kind == 'json' → body_data is a dict; /gate returns it as JSON.
    """
    cookie_value: str
    body_kind: Literal["ndjson_stream", "ndjson_bytes", "json"]
    body_data: Any


# ────────────────────────────────────────────────────────────────────
# Helpers (moved from app.py — they belong with the orchestrator that
# uses them, not with HTTP plumbing).
# ────────────────────────────────────────────────────────────────────


def _assemble_session_token_value(user_object: UserObject) -> str:
    """Assemble the 67-byte ch_session cookie value per
    EPIC-002-F-003-S-003-REQ-B-007: GUID(32) + first_stamp(17) + 'X'
    + second_stamp(17).
    """
    st = user_object.current_session_token
    guid = st.get_auth_token()              # 32 bytes
    nonce_field = st.get_nonce()            # 17 + 1 + 17 = 35 bytes
    return f"{guid}{nonce_field}"           # 67 bytes


def _time_remaining_seconds(user_object: UserObject) -> int:
    """REQ-T-004: floor((most_recent_restamp + 300s) - now) in seconds.
    Clipped to non-negative integers."""
    nonce_field = user_object.current_session_token.get_nonce()
    latest = nonce_field[18:]
    try:
        secs = datetime.strptime(latest[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        ms = int(latest[14:17])
        restamp_ms = int(secs.timestamp() * 1000) + ms
    except Exception:
        return 0
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    remaining_ms = (restamp_ms + 300_000) - now_ms
    return max(0, remaining_ms // 1000)


def _was_registered(user_object: UserObject) -> bool:
    """REQ-T-010: tiny derived flag for the browser's timeout copy."""
    return bool(getattr(user_object, "is_registered", False))


def _session_token_wire(user_object: UserObject) -> dict:
    """Cryptographic-display projection of the session token.

    Surfaces the bare SessionToken — the three display fields the
    panels render (signed token, nonce, GUID) plus the SessionToken
    envelope (origin, signature, created_at, signed, server_env,
    last_used) needed by the existing /session + /verify-token chain.
    """
    st = user_object.current_session_token
    if hasattr(st, "model_dump"):
        return st.model_dump(mode="python", exclude_none=False)
    return dict(st)


# ────────────────────────────────────────────────────────────────────
# Splash data — two buckets shipped verbatim to splash_render.js. The
# dialogue narrative + the system-action log are independent threads in
# the FE; each carries its own per-session sequence number.
# ────────────────────────────────────────────────────────────────────


def _splash_data(user_object) -> dict[str, Any]:
    cst = user_object.current_session_token
    identity = {
        "user_type": getattr(user_object, "user_type", None) or "Guest",
        "is_registered": bool(getattr(user_object, "is_registered", False)),
        "public_username": getattr(user_object, "public_username", None) or "",
        "OAuthIdentities": getattr(user_object, "OAuthIdentities", []) or [],
        "guid": cst.get_auth_token(),
        "origin": cst.origin,
        "server_env": cst.server_env,
        "created_at": str(cst.created_at) if cst.created_at is not None else "",
        "expires_at": str(user_object.expires_at) if user_object.expires_at is not None else "",
    }
    sch = user_object.session_conversation_history.model_dump()
    utterances = sch.get("utterances") if isinstance(sch, dict) else []
    actions = sch.get("actions") if isinstance(sch, dict) else []
    return {
        "identity": identity,
        "threads": {
            "empty": not utterances and not actions,
            "utterances": utterances or [],
            "actions": actions or [],
        },
    }


# ────────────────────────────────────────────────────────────────────
# Op handlers — graph nodes
# ────────────────────────────────────────────────────────────────────

async def _handle_boot(deps: AgentDeps, payload: dict[str, Any]) -> Response:
    deps.stream({"kind": "boot", "data": {"ok": True}})
    return Response(kind="boot", result={"op": "boot"})


async def _handle_splash(deps: AgentDeps, payload: dict[str, Any]) -> Response:
    data = _splash_data(deps.user_object)
    append_action(
        deps.user_object,
        tool_name="splash_displayed",
        input_json={},
        output_json={"note": "SharedServices took ownership and rendered the User Object."},
    )
    deps.stream({"kind": "splash", "data": data})
    return Response(kind="splash", result=data)


async def _handle_record_ux_event(deps: AgentDeps, payload: dict[str, Any]) -> Response:
    event_type = str(payload.get("event_type") or "").strip()
    if not event_type:
        return Response(kind="record_ux_event", result={"ok": False, "error": "event_type required"})
    append_action(
        deps.user_object,
        tool_name="ux_event",
        input_json={
            "event_type": event_type,
            "value": payload.get("value"),
        },
        output_json={},
    )
    deps.stream({"kind": "ux_event_recorded", "data": {"event_type": event_type}})
    return Response(kind="record_ux_event", result={"ok": True})


async def _handle_utterance(deps: AgentDeps, payload: dict[str, Any]) -> Response:
    """UR dispatch for op == 'utterance'. Routes the user's typed text
    through UtteranceManager (which classifies and writes IntentDocument
    to user_object.intent), then pattern-matches on target_action and
    dispatches the appropriate tool.

    UR validates schema + semantic compliance before dispatch; raises if
    UM gave us a malformed document. The tool dispatched is the only one
    that should write user-facing output for that intent.
    """
    text = str(payload.get("text") or "").strip()
    if not text:
        return Response(kind="utterance", result={"ok": False, "error": "text required"})

    # Persist the utterance onto user_object so UM reads it from the
    # canonical place (session_conversation_history.utterances[-1]).
    deps.user_object.persist_user_state(text)

    from UtteranceManager import manager as utterance_manager_module
    await utterance_manager_module.TOOL.run_and_log(
        deps, utterance_manager_module.Request(),
    )

    last_target_action: Optional[str] = None
    for _hop in range(_MAX_DISPATCH_HOPS):
        document = deps.user_object.intent
        if document is None:
            raise RuntimeError(
                "UR compliance: UtteranceManager returned without setting "
                "user_object.intent"
            )

        target_action = document.target_action
        if target_action == last_target_action:
            break

        # REQ-B-004: do not chain to closeConnection200 on a turn that
        # carries a pending disambiguation — the connection is logically
        # still open across the user's next turn.
        if target_action == "closeConnection200" and _any_pending_disambiguation(document):
            _log.debug(
                "UR: suppressing closeConnection200 chain because at least "
                "one intent carries pending_disambiguation"
            )
            break

        _validate_document(document, target_action)
        await _dispatch_target_action(deps, document, target_action)
        last_target_action = target_action
    else:
        raise RuntimeError(
            f"UR dispatch exceeded {_MAX_DISPATCH_HOPS} hops; last "
            f"target_action={last_target_action!r}"
        )

    return Response(
        kind="utterance",
        result={"target_action": last_target_action},
    )


def _any_pending_disambiguation(document) -> bool:
    """True when any intent entry on the document carries a
    pending_disambiguation marker. UR uses this to suppress closeConnection200
    chaining (REQ-B-004) so the connection remains logically open across the
    user's next turn."""
    for entry in document.intents:
        if getattr(entry, "pending_disambiguation", None) is not None:
            return True
    return False


_MAX_DISPATCH_HOPS = 3


def _validate_document(document, target_action: str) -> None:
    """UR compliance check on the IntentDocument: target_action enumerated
    by Pydantic; must correspond to a name in intents[]; required arguments
    non-empty and parseable; findAProvider geography sufficiency."""
    import json as _json

    target_intent_entry = next(
        (i for i in document.intents if i.name == target_action), None,
    )
    if target_intent_entry is None:
        raise RuntimeError(
            f"UR compliance: target_action {target_action!r} has no matching "
            f"entry in intents[] (names={[i.name for i in document.intents]})"
        )
    for arg in target_intent_entry.arguments:
        if arg.required and not arg.value:
            raise RuntimeError(
                f"UR compliance: required argument {arg.name!r} for target_action "
                f"{target_action!r} has empty value"
            )
        if arg.type == "boolean" and arg.value not in ("true", "false"):
            raise RuntimeError(
                f"UR compliance: boolean argument {arg.name!r} has value "
                f"{arg.value!r}, must be 'true' or 'false'"
            )
        if arg.type in ("object", "array"):
            try:
                _json.loads(arg.value)
            except _json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"UR compliance: {arg.type} argument {arg.name!r} value is not "
                    f"valid JSON: {exc}"
                )

    if target_action == "findAProvider":
        geo_arg = next(
            (a for a in target_intent_entry.arguments if a.name == "geography"), None,
        )
        if geo_arg is None:
            raise RuntimeError(
                "UR compliance: findAProvider missing geography argument"
            )
        geo = _json.loads(geo_arg.value)
        zip_code = (geo.get("zip") or "").strip()
        state = (geo.get("state") or "").strip()
        if not zip_code and not state:
            raise RuntimeError(
                "UR compliance: findAProvider geography insufficient — needs "
                "zip OR state (city/county without state are not enough)"
            )


async def _dispatch_target_action(deps: AgentDeps, document, target_action: str) -> None:
    """Dispatch the tool that owns this target_action. Tools may mutate
    user_object.intent before returning; the caller loops and re-dispatches."""
    import json as _json

    target_intent_entry = next(
        (i for i in document.intents if i.name == target_action), None,
    )

    if target_action == "nonsense":
        from NonsenseTool import nonsense_tool
        await nonsense_tool.TOOL.run_and_log(deps, nonsense_tool.Request())

    elif target_action == "closeConnection200":
        from CloseConnection200Tool import close_connection_200_tool
        await close_connection_200_tool.TOOL.run_and_log(
            deps, close_connection_200_tool.Request(),
        )

    elif target_action == "specialtySearch":
        complaint = next(
            (a.value for a in target_intent_entry.arguments if a.name == "complaint"),
            "",
        )
        specialties = await _run_or_cache_specialty_filter(deps, complaint)
        if not specialties:
            deps.stream({
                "kind": "final",
                "data": {"ok": False, "error": "no_specialties"},
            })
        else:
            deps.stream({"kind": "final", "data": {"ok": True}})

    elif target_action == "findAProvider":
        from authentication import provider_search_and_selection_tool

        complaint = next(
            (a.value for a in target_intent_entry.arguments if a.name == "complaint"),
            "",
        )
        specialties = await _run_or_cache_specialty_filter(deps, complaint)
        if not specialties:
            deps.stream({
                "kind": "final",
                "data": {"ok": False, "error": "no_specialties"},
            })
        else:
            # FindCare-UR REQ-B-002: ProviderSearch fires only when
            # geography is sufficient.
            geo_arg_val = next(
                (a for a in target_intent_entry.arguments if a.name == "geography"),
                None,
            )
            geo = _json.loads(geo_arg_val.value) if geo_arg_val else {}
            ps_req = provider_search_and_selection_tool.Request(
                specialty_codes=[c["code"] for c in specialties],
                state=geo.get("state"),
                city=geo.get("city"),
                county=geo.get("county"),
                zip=geo.get("zip"),
                limit=25,
            )
            await provider_search_and_selection_tool.TOOL.run_and_log(
                deps, ps_req,
            )
            deps.stream({"kind": "final", "data": {"ok": True}})

    else:
        raise RuntimeError(
            f"UR compliance: out-of-catalog target_action {target_action!r}"
        )


async def _run_or_cache_specialty_filter(deps: AgentDeps, complaint: str) -> list[dict]:
    """REQ-B-002 + REQ-B-003 + FindCare-UR REQ-B-001.

    Look for a cached nucc_codes argument on the IntentDocument's
    specialtySearch and findAProvider entries. On a cache hit, parse
    the cached value and stream it back to the FE as a
    {kind:"specialties"} event without re-running SpecialtyFilter.
    On a cache miss, run SpecialtyFilter, then write nucc_codes back
    onto both intent entries (whichever exist) so subsequent turns hit
    the cache.

    Returns the list of {code, name, score, ...} dicts.
    """
    import json as _json
    from UtteranceManager.intent_document import Argument

    document = deps.user_object.intent
    if document is None:
        return []

    cached = _read_nucc_codes_cache(document)
    if cached is not None:
        # Stream the cached codes to the FE so the specialty filter renders
        # on this turn without re-running SpecialtyFilter.
        deps.stream({"kind": "specialties", "data": {"specialties": cached}})
        return cached

    from SpecialtyFilter import specialty_filter_tool
    fs = await specialty_filter_tool.TOOL.run_and_log(
        deps, specialty_filter_tool.Request(query=complaint),
    )
    if fs.error or not fs.specialties:
        return []

    specialties = [s.model_dump(exclude_none=True) for s in fs.specialties]
    encoded = _json.dumps(specialties)

    # Write nucc_codes back onto every applicable intent entry so the
    # next turn (after the user resolves a pending disambiguation) sees
    # the cached value. Rebuild each affected entry through Pydantic so
    # validation runs on the new argument list.
    new_intents = []
    for entry in document.intents:
        if entry.name in ("specialtySearch", "findAProvider"):
            kept_args = [a for a in entry.arguments if a.name != "nucc_codes"]
            kept_args.append(Argument(
                name="nucc_codes", value=encoded, type="array", required=False,
            ))
            new_intents.append(type(entry).model_validate({
                **entry.model_dump(),
                "arguments": [a.model_dump() for a in kept_args],
            }))
        else:
            new_intents.append(entry)
    from UtteranceManager.intent_document import IntentDocument as _IntentDocument
    deps.user_object.intent = _IntentDocument(
        target_action=document.target_action,
        intents=new_intents,
        user_message=document.user_message,
    )

    return specialties


def _read_nucc_codes_cache(document) -> Optional[list[dict]]:
    """Return the parsed nucc_codes list cached on any specialty/find
    intent entry, or None if no cache hit. Prefers specialtySearch's
    cache (the first place SpecialtyFilter writes to today)."""
    import json as _json

    for name in ("specialtySearch", "findAProvider"):
        entry = next((i for i in document.intents if i.name == name), None)
        if entry is None:
            continue
        for arg in entry.arguments:
            if arg.name == "nucc_codes" and arg.value:
                try:
                    parsed = _json.loads(arg.value)
                except _json.JSONDecodeError:
                    continue
                if isinstance(parsed, list) and parsed:
                    return parsed
    return None


async def _handle_provider_detail(deps: AgentDeps, payload: dict[str, Any]) -> Response:
    """Click path: a user clicked the detail link on a provider card. This
    is NOT an utterance — no LLM hop. Forward the card-field payload to
    the ProviderDetail tool, which HTTPS-hops to FindCare and streams
    {kind: 'provider-detail', data: ...} back to the FE."""
    req = provider_detail_tool.Request(**payload)
    resp = await provider_detail_tool.TOOL.run_and_log(deps, req)
    deps.stream({"kind": "final", "data": {"ok": not resp.error}})
    return Response(kind="provider-detail", result=resp.model_dump(exclude_none=True))


_HANDLERS = {
    "boot": _handle_boot,
    "splash": _handle_splash,
    "record_ux_event": _handle_record_ux_event,
    "utterance": _handle_utterance,
    "provider-detail": _handle_provider_detail,
}


# ────────────────────────────────────────────────────────────────────
# Tool class — single dispatch entry point for the gate
# ────────────────────────────────────────────────────────────────────

class UniversalNavigationTool(ChatHealthyTool):
    """Receives the post-AuthN deps + a typed NavRequest (op + payload),
    dispatches to the right graph-node handler. The handler may invoke
    other tools (via their `run_and_log()`); those calls auto-log to
    `deps.user_object.session_conversation_history` so the splash render
    finds the per-tool invocation entries with `kind:"tool_invocation"`.
    """
    TOOL_NAME = "universal_navigation"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        op = (request.op or "boot")
        handler = _HANDLERS.get(op)
        if handler is None:
            return self.Response(kind="unknown_op", result={"op": op, "error": f"unknown op {op!r}"})
        return await handler(deps, request.payload or {})

    async def handle_gate(self, gate_req: GateRequest) -> GateResponse:
        """The whole /gate request lifecycle minus the HTTP envelope.

        `/gate` is now thin HTTP plumbing: it parses the body + cookie,
        constructs a `GateRequest`, calls this method, and turns the
        returned `GateResponse` into a FastAPI Response. Everything
        else — wire-intent validation, session load, AuthN dispatch,
        OAuth-start URL manufacture, cookie value assembly, AgentDeps
        construction, op dispatch, stream feed, AUTHN persist, final
        event construction — lives here.
        """
        # 1. Wire-intent validation (gateway concern).
        if gate_req.intent is not None and gate_req.intent not in _KNOWN_WIRE_INTENTS:
            raise ValueError(
                f"/gate: unknown intent {gate_req.intent!r}; expected one of "
                f"{sorted(_KNOWN_WIRE_INTENTS)} or absent"
            )

        # 2. Mongo handle + AuthnDeps.
        mongo_frontend = authn.get_mongo_frontend()
        authn_deps = AuthnDeps(
            prior_guid=gate_req.prior_guid,
            server_env=_ENV,
            mongo_frontend=mongo_frontend,
        )

        # 3. Session load + auth_intent decision.
        loaded_user_object: Optional[UserObject] = None
        if gate_req.prior_guid:
            sessions_coll = mongo_frontend[authn._SESSION_DB][authn._SESSION_COLLECTION]
            session_doc = sessions_coll.find_one({"_id": gate_req.prior_guid})
            if session_doc:
                try:
                    candidate = UserObject.model_validate(
                        {k: v for k, v in session_doc.items() if k != "_id"}
                    )
                    expires_at = candidate.expires_at
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    if expires_at > datetime.now(timezone.utc):
                        loaded_user_object = candidate
                except Exception as exc:
                    _log.warning(
                        "could not reconstitute UserObject for %s: %s",
                        gate_req.prior_guid[:8], exc,
                    )

        if loaded_user_object is not None:
            auth_intent = "manage_session"
            inbound_user_object = loaded_user_object
            sch_dump = loaded_user_object.session_conversation_history.model_dump()
            intent_dump = (
                loaded_user_object.intent.model_dump()
                if loaded_user_object.intent is not None else None
            )
            _log.debug(
                "handle_gate SESSION LOADED prior_guid=%s utterances=%d actions=%d "
                "has_intent=%s\nLOADED session_conversation_history=%s\n"
                "LOADED intent=%s",
                gate_req.prior_guid[:8] + "..." if gate_req.prior_guid else None,
                len(loaded_user_object.session_conversation_history.utterances),
                len(loaded_user_object.session_conversation_history.actions),
                loaded_user_object.intent is not None,
                _json.dumps(sch_dump, default=str),
                _json.dumps(intent_dump, default=str),
            )
        else:
            auth_intent = "manufacture_session"
            inbound_user_object = UserObject(
                current_session_token="NULL",
                expires_at=datetime.now(timezone.utc)
                + timedelta(seconds=_SESSION_TTL_SECONDS),
            )
            _log.debug(
                "handle_gate FRESH MINT (no prior_guid or session not found) "
                "prior_guid=%s",
                gate_req.prior_guid[:8] + "..." if gate_req.prior_guid else None,
            )

        # 4. Call AUTHN_TOOL.run.
        authn_resp = await authn.TOOL.run(
            authn_deps,
            authn.Request(intent=auth_intent, user_object=inbound_user_object),
        )
        user_object = authn_resp.user_object
        fresh_mint = authn_resp.fresh_mint
        guid = user_object.current_session_token.get_auth_token()
        cookie_value = _assemble_session_token_value(user_object)

        # 5. Login_register short-circuit. The gateway-level OAuth start
        #    URL is built here (HTTP knowledge stays out of the auth tool).
        if gate_req.intent == _WIRE_INTENT_LOGIN_REGISTER:
            shared = _ENV_TO_SHARED_URL.get(_ENV)
            if not shared:
                raise RuntimeError(
                    f"/gate: no SharedServices URL for env {_ENV!r}"
                )
            redirect_url = f"{shared}/auth/google/start?session_guid={guid}"
            try:
                await authn.TOOL.persist(authn_deps, user_object, fresh_mint)
            except Exception as exc:
                _log.exception("AuthN.persist (redirect path) failed: %s", exc)
            redirect_event = {
                "type": "redirect",
                "url": redirect_url,
                "time_remaining_seconds": _time_remaining_seconds(user_object),
                "was_registered": _was_registered(user_object),
                "session_token": _session_token_wire(user_object),
            }
            if gate_req.want_ndjson:
                body_bytes = (
                    _json.dumps(redirect_event, default=str) + "\n"
                ).encode("utf-8")
                return GateResponse(
                    cookie_value=cookie_value,
                    body_kind="ndjson_bytes",
                    body_data=body_bytes,
                )
            return GateResponse(
                cookie_value=cookie_value,
                body_kind="json",
                body_data=redirect_event,
            )

        # 6. Build event queue + stream sink + AgentDeps + nav.Request.
        event_queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        def stream_sink(event: dict) -> None:
            event_queue.put_nowait(event)

        agent_deps = AgentDeps(
            user_object=user_object,
            session_token=user_object.current_session_token,
            mongo_frontend=mongo_frontend,
            server_env=_ENV,
            stream=stream_sink,
        )
        nav_req = self.Request(op=gate_req.op, payload=gate_req.payload)

        # 7. Pipeline coroutine: run handler dispatch, persist, push the
        #    final_event, push the sentinel. Wrapped in try/finally so the
        #    sentinel is always pushed even if the orchestration crashes
        #    (otherwise the stream consumer would hang).
        async def _run_pipeline_then_finalize() -> None:
            nav_exc_local: Optional[BaseException] = None
            res_local: Optional[Response] = None
            try:
                try:
                    res_local = await self.run(agent_deps, nav_req)
                except Exception as exc:
                    nav_exc_local = exc
                    _log.exception(
                        "UniversalNavigation run failed for op=%s payload=%r: %s",
                        gate_req.op, gate_req.payload, exc,
                    )

                session_token_proj = _session_token_wire(user_object)
                if nav_exc_local is not None:
                    final_event_local = {
                        "kind": "final", "ok": False,
                        "error": f"{type(nav_exc_local).__name__}: {nav_exc_local}",
                        "guid": guid,
                        "time_remaining_seconds": _time_remaining_seconds(user_object),
                        "was_registered": _was_registered(user_object),
                        "session_token": session_token_proj,
                    }
                else:
                    final_event_local = {
                        "kind": "final", "ok": True,
                        "guid": guid,
                        "result": res_local.result if res_local else {},
                        "result_kind": res_local.kind if res_local else "unknown",
                        "time_remaining_seconds": _time_remaining_seconds(user_object),
                        "was_registered": _was_registered(user_object),
                        "session_token": session_token_proj,
                    }

                try:
                    sch_dump = user_object.session_conversation_history.model_dump()
                    intent_dump = (
                        user_object.intent.model_dump()
                        if user_object.intent is not None else None
                    )
                    _log.debug(
                        "handle_gate PERSIST guid=%s utterances=%d actions=%d "
                        "has_intent=%s fresh_mint=%s\n"
                        "PERSIST session_conversation_history=%s\n"
                        "PERSIST intent=%s",
                        guid[:8] + "...",
                        len(user_object.session_conversation_history.utterances),
                        len(user_object.session_conversation_history.actions),
                        user_object.intent is not None,
                        fresh_mint,
                        _json.dumps(sch_dump, default=str),
                        _json.dumps(intent_dump, default=str),
                    )
                    await authn.TOOL.persist(authn_deps, user_object, fresh_mint)
                except Exception as exc:
                    _log.exception("AuthN.persist failed: %s", exc)

                event_queue.put_nowait(final_event_local)
            finally:
                event_queue.put_nowait(_SENTINEL)

        # 8. NDJSON streaming: schedule the pipeline as a background task;
        #    return an async iterator that the FastAPI StreamingResponse
        #    will consume chunk-by-chunk.
        if gate_req.want_ndjson:
            asyncio.create_task(_run_pipeline_then_finalize())

            async def _event_stream() -> AsyncIterator[bytes]:
                while True:
                    item = await event_queue.get()
                    if item is _SENTINEL:
                        return
                    yield (_json.dumps(item, default=str) + "\n").encode("utf-8")

            return GateResponse(
                cookie_value=cookie_value,
                body_kind="ndjson_stream",
                body_data=_event_stream(),
            )

        # 9. Non-NDJSON: run inline, drain queue for the final_event.
        await _run_pipeline_then_finalize()
        final_event: Optional[dict] = None
        while True:
            item = await event_queue.get()
            if item is _SENTINEL:
                break
            if isinstance(item, dict) and item.get("kind") == "final":
                final_event = item
        return GateResponse(
            cookie_value=cookie_value,
            body_kind="json",
            body_data=final_event or {},
        )


TOOL = UniversalNavigationTool()
