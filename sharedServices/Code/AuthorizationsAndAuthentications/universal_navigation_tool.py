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

UR is a class: orchestration logic lives as methods on
UniversalNavigationTool so each component has its own encapsulation.
Pure utility functions (session-token assembly, splash data shape,
small read-only helpers) stay module-level since they hold no
orchestration state and are referenced from non-orchestration code
paths.
"""
from __future__ import annotations

import asyncio
import json as json
from chathealthy_lib import ChatHealthyLoggingService
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Literal, Optional, Union

from pydantic import BaseModel, Field

from chathealthy_lib.authentication.agent_deps import (
    AgentDeps,
    AuthnDeps,
    append_action,
)
from chathealthy_lib.authentication.chathealthy_tool import ChatHealthyTool
from authentication import (
    authorizations_and_authentications_tool as authn,
    evalcare_splash_tool,
    provider_detail_tool,
)
from chathealthy_lib.authentication.user_object import UserObject
from chathealthy_lib import ChatHealthyException
from UtteranceManager import utterance_manager
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

log = ChatHealthyLoggingService()

TOOL_NAME = "universal_navigation"

# ────────────────────────────────────────────────────────────────────
# Wire-level constants — gateway-level concerns that live with the
# orchestrator since /gate is now thin HTTP plumbing only.
# ────────────────────────────────────────────────────────────────────

ENV = os.getenv("ENV_PREFIX", "dev")
SESSION_TTL_SECONDS = 300

WIRE_INTENT_UTTERANCE = "utterance"
KNOWN_WIRE_INTENTS = frozenset({WIRE_INTENT_UTTERANCE})
# login_register removed from /gate per S-004 rewire: the Login &
# Registration nav button is now a form-target popup posting directly
# to /auth/google/start, which routes through OAuthLoginTool (start
# phase). /gate is for utterance traffic only.

ENV_TO_SHARED_URL = {
    "dev":   "https://dev-hf.chathealthy.ai",
    "qa":    "https://skipsnow-qa-sharedservicesspace.hf.space",
    "prod":  "https://skipsnow-sharedservicesspace.hf.space",
    "local": "https://localhost:8002",
}

MAX_DISPATCH_HOPS = 3


# ────────────────────────────────────────────────────────────────────
# Request / Response contracts
# ────────────────────────────────────────────────────────────────────


def _ch_exc():
    """ChatHealthyException without assuming the library is installed.
    These modules run as bare scripts in the devops chain."""
    import sys as _s, pathlib as _p
    for _d in _p.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _l = _d / "ChatHealthyLib" / "src"
            if str(_l) not in _s.path:
                _s.path.insert(0, str(_l))
            break
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException


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
    session_guid: Optional[str] = None
    want_ndjson: bool = False
    # Pulled by /gate from X-Forwarded-For (preferred — Cloudflare and HF
    # proxies populate it with the real client) or scope.client.host. UR
    # stamps it onto user_object.ip_address before any tool dispatch so
    # SafetyLockoutTool reads it from session state, not from the HTTP
    # layer.
    client_ip: Optional[str] = None


@dataclass
class GateResponse:
    """What the orchestrator returns to /gate.

    body_kind == 'ndjson_stream' → body_data is an async iterator of
    bytes; /gate returns StreamingResponse(body_data).

    body_kind == 'ndjson_bytes' → body_data is a single bytes blob (e.g.,
    the redirect event serialized as one NDJSON line); /gate returns
    Response(content=body_data, media_type='application/x-ndjson').

    body_kind == 'json' → body_data is a dict; /gate returns it as JSON.

    Session continuity is carried client-side: ClientRouter holds the
    session GUID in JavaScript memory and threads it as body.session_guid
    on every /gate POST. No HTTP cookie is set or read.
    """
    body_kind: Literal["ndjson_stream", "ndjson_bytes", "json"]
    body_data: Any


# ────────────────────────────────────────────────────────────────────
# Pure utility helpers (no orchestration state; referenced from /gate
# and from UR methods). Kept module-level so non-orchestration callers
# (login-register short-circuit, final-event projection) can use them
# without instantiating UR.
# ────────────────────────────────────────────────────────────────────


def time_remaining_seconds(user_object: UserObject) -> int:
    """REQ-T-004: floor((most_recent_restamp + 300s) - now) in seconds.
    Clipped to non-negative integers."""
    nonce_field = user_object.current_session_token.get_nonce()
    latest = nonce_field[18:]
    try:
        secs = datetime.strptime(latest[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        ms = int(latest[14:17])
        restamp_ms = int(secs.timestamp() * 1000) + ms
    except Exception as _exc:
        # Mode 2 (REQ-B-008): nonce parse failure. Per operator (Skip 2026-
        # 06-22): set time remaining back to FULL (300s) AND demote the
        # user to guest if currently registered — corrupt nonce must not
        # silently expire a logged-in user. Mode 2 because demoting a
        # registered user is user-affecting; operator must always see it.
        log.error("nonce parse failed: %s", _exc, exc=ChatHealthyException(
                                                     mode="nonce_parse_failed",
                                                     message=f"nonce parse failed (resetting to full guest session): {_exc}",
                                                     component="UniversalNavigationTool",
                                                     exception=_exc,
                                                 ), if_not_debug_log=True)
        if getattr(user_object, "is_registered", False):
            user_object.is_registered = False
        return 300
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    remaining_ms = (restamp_ms + 300_000) - now_ms
    return max(0, remaining_ms // 1000)


def was_registered(user_object: UserObject) -> bool:
    """REQ-T-010: tiny derived flag for the browser's timeout copy."""
    return bool(getattr(user_object, "is_registered", False))


def session_token_wire(user_object: UserObject) -> dict:
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


def splash_data(user_object) -> dict[str, Any]:
    """Splash payload shape. Read-only projection of user_object."""
    cst = user_object.current_session_token
    oauth_idents = [
        oi.model_dump(mode="json") if hasattr(oi, "model_dump") else oi
        for oi in (getattr(user_object, "OAuthIdentities", []) or [])
    ]
    identity = {
        "user_type": getattr(user_object, "user_type", None) or "Guest",
        "is_registered": bool(getattr(user_object, "is_registered", False)),
        "public_username": getattr(user_object, "public_username", None) or "",
        "OAuthIdentities": oauth_idents,
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


def any_pending_disambiguation(document) -> bool:
    """True when any intent entry on the document carries a
    pending_disambiguation marker. UR uses this to suppress closeConnection200
    chaining (REQ-B-004) so the connection remains logically open across the
    user's next turn."""
    for entry in document.intents:
        if getattr(entry, "pending_disambiguation", None) is not None:
            return True
    return False


def read_nucc_codes_cache(document) -> Optional[list[dict]]:
    """Return the parsed nucc_codes list cached on any specialty/find
    intent entry, or None if no cache hit. Prefers specialtySearch's
    cache (the first place SpecialtyFilter writes to today)."""
    import json as json

    for name in ("specialtySearch", "findAProvider"):
        entry = next((i for i in document.intents if i.name == name), None)
        if entry is None:
            continue
        for arg in entry.arguments:
            if arg.name == "nucc_codes" and arg.value:
                try:
                    parsed = json.loads(arg.value)
                except json.JSONDecodeError as _exc:
                    # Mode 1 (REQ-B-008): intent arg JSON malformed; UR skips
                    # the arg and continues. log.info + default debug-gated.
                    log.info("intent arg JSON decode failed (skipped): %s", _exc, exc=ChatHealthyException(
                                                                                      mode="intent_arg_json_decode_failed",
                                                                                      message=f"intent arg JSON decode failed (skipped): {_exc}",
                                                                                      component="UniversalNavigationTool",
                                                                                      exception=_exc,
                                                                                  ), if_not_debug_log=True)
                    continue
                if isinstance(parsed, list) and parsed:
                    return parsed
    return None


# ────────────────────────────────────────────────────────────────────
# Tool class — single dispatch entry point for the gate. UR's
# orchestration helpers live as methods so each component is
# self-contained and the class is the unit of testing and extension.
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

    # Map ops to method names. run() looks up by op and dispatches via
    # getattr(self, name). Adding a new op = new method + new dict entry.
    _OP_HANDLERS = {
        "boot":                 "_handle_boot",
        "splash":               "_handle_splash",
        "record_ux_event":      "_handle_record_ux_event",
        "utterance":            "_handle_utterance",
        "provider-detail":      "_handle_provider_detail",
        "apply_filter":         "_handle_apply_filter",
        "evalcare-splash":      "_handle_evalcare_splash",
        # clinical_trials_page op removed: client-side cache pagination
        # eliminated the per-page server round-trip; the React widget
        # slices its cached chunks locally.
        "about_chathealthy":    "_handle_about_chathealthy",
        "provider_selection":   "_handle_provider_selection",
        "clinical_trial_selection": "_handle_clinical_trial_selection",
        "claim_oauth_result":   "_handle_claim_oauth_result",
    }

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        op = (request.op or "boot")
        method_name = self._OP_HANDLERS.get(op)
        if method_name is None:
            return self.Response(
                kind="unknown_op",
                result={"op": op, "error": f"unknown op {op!r}"},
            )
        return await getattr(self, method_name)(deps, request.payload or {})

    # ── Op handlers ───────────────────────────────────────────────

    async def _handle_boot(self, deps: AgentDeps, payload: dict[str, Any]) -> Response:
        deps.stream({"kind": "boot", "data": {"ok": True}})
        return Response(kind="boot", result={"op": "boot"})

    async def _handle_splash(self, deps: AgentDeps, payload: dict[str, Any]) -> Response:
        data = splash_data(deps.user_object)
        append_action(
            deps.user_object,
            tool_name="splash_displayed",
            input_json={},
            output_json={"note": "SharedServices took ownership and rendered the User Object."},
        )
        deps.stream({"kind": "splash", "data": data})
        return Response(kind="splash", result=data)

    async def _handle_record_ux_event(self, deps: AgentDeps, payload: dict[str, Any]) -> Response:
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

    async def _handle_utterance(self, deps: AgentDeps, payload: dict[str, Any]) -> Response:
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

        # Persist the utterance onto user_object so downstream tools (UM or
        # LockoutTool) read it from the canonical place
        # (session_conversation_history.utterances[-1]).
        deps.user_object.persist_user_state(text)

        # safetyLockout pre-UM gate. UR hydrates user_object.is_locked_out +
        # user_object.lockout from {env}_Safety.emergency_incidents by IP. If
        # the IP has an active (unexpired, not-unlocked) record, UM is NOT
        # called this turn — LockoutTool runs directly to handle Task A
        # (operator $unlock backdoor) or Task B (still-locked reminder).
        await self._hydrate_lockout_if_any(deps)
        if deps.user_object.is_locked_out:
            from LockoutTool import lockout_tool
            await lockout_tool.TOOL.run_and_log(deps, lockout_tool.Request())
            return Response(kind="utterance", result={"target_action": "safetyLockout"})

        from UtteranceManager import utterance_manager as utterance_manager_module
        try:
            await utterance_manager_module.TOOL.run_and_log(
                deps, utterance_manager_module.Request(),
            )
        except ChatHealthyException as exc:
            if exc.mode != "llm_unavailable":
                raise
            await self._dispatch_llm_unavailable_dialogue(deps, exc)
            return Response(
                kind="utterance",
                result={"target_action": "closeConnection200",
                        "mode": "llm_unavailable"},
            )

        # "New is new" invariant: a fresh free-text utterance clears any
        # selected_nucc_codes carried over from a prior turn's apply_filter
        # gesture. selected_nucc_codes exists on the IntentDocument ONLY as
        # the immediate result of an apply_filter; it does not survive a
        # subsequent free-text utterance. Without this strip the prior
        # turn's filter selection silently narrows ProviderSearch on the
        # new query (reported by operator 2026-06-11).
        document = deps.user_object.intent
        if document is not None:
            for entry in document.intents:
                entry.arguments = [
                    a for a in entry.arguments if a.name != "selected_nucc_codes"
                ]

        last_target_action: Optional[str] = None
        for _hop in range(MAX_DISPATCH_HOPS):
            document = deps.user_object.intent
            if document is None:
                raise ChatHealthyException(
            mode="runtime_error",
            component="universal_navigation_tool",
            message="UR compliance: UtteranceManager returned without setting "
                    "user_object.intent")

            target_action = document.target_action
            if target_action == last_target_action:
                break

            # REQ-B-004: do not chain to closeConnection200 on a turn that
            # carries a pending disambiguation — the connection is logically
            # still open across the user's next turn.
            if target_action == "closeConnection200" and any_pending_disambiguation(document):
                break

            self._validate_document(document, target_action)
            await self._dispatch_target_action(deps, document, target_action)
            last_target_action = target_action
        else:
            raise ChatHealthyException(
            mode="runtime_error",
            component="universal_navigation_tool",
            message=f"UR dispatch exceeded {MAX_DISPATCH_HOPS} hops; last "
                f"target_action={last_target_action!r}")

        return Response(
            kind="utterance",
            result={"target_action": last_target_action},
        )

    async def _handle_provider_detail(self, deps: AgentDeps, payload: dict[str, Any]) -> Response:
        """Click path: a user clicked the detail link on a provider card. This
        is NOT an utterance — no LLM hop. Forward the card-field payload to
        the ProviderDetail tool, which HTTPS-hops to FindCare and streams
        {kind: 'provider-detail', data: ...} back to the FE."""
        req = provider_detail_tool.Request(**payload)
        resp = await provider_detail_tool.TOOL.run_and_log(deps, req)
        # No inner "final" emission — _run_pipeline_then_finalize emits
        # the single canonical final event with full payload.
        return Response(kind="provider-detail", result=resp.model_dump(exclude_none=True, mode='json'))

    async def _handle_evalcare_splash(self, deps: AgentDeps, payload: dict[str, Any]) -> Response:
        """Click path: a user clicked the EvaluateCare banner button. This
        is NOT an utterance — no LLM hop. Server-to-server HTTPS-hops to
        EvaluateCare's /splash and streams {kind: 'evalcare-splash', ...}
        back to the FE. Mirror of _handle_provider_detail."""
        req = evalcare_splash_tool.Request(**payload)
        resp = await evalcare_splash_tool.TOOL.run_and_log(deps, req)
        # No inner "final" emission — _run_pipeline_then_finalize emits
        # the single canonical final event with full payload.
        return Response(kind="evalcare-splash", result=resp.model_dump(exclude_none=True))

    async def _handle_apply_filter(self, deps: AgentDeps, payload: dict[str, Any]) -> Response:
        """UR dispatch for op == 'apply_filter' per
        EPIC-002-F-010-S-002-REQ-B-002.

        The FE just told us the user clicked Apply Filter with a new
        specialty selection (payload carries the new nucc_codes set).
        UR reads the carried IntentDocument's geography and branches:

        * Geography sufficient (zip OR state, per the same rule
          findAProvider applies) — build a new IntentDocument with
          target_action=findAProvider using the new nucc_codes set,
          dispatch ProviderSearch directly. UM is NOT called (no prose
          needed; the provider list IS the response).

        * Geography insufficient — populate
          manufacture_utterance_reason with the relevant facts and
          dispatch UM as a manufacture-trigger Request per
          EPIC-002-F-010-S-003-REQ-T-001. UM authors the
          context-sensitive location prompt and morphs the
          IntentDocument to closeConnection200; UR's bounded dispatch
          loop chains to CloseConnection200Tool.
        """
        import json as json
        from chathealthy_lib.authentication.intent_document import (
            Argument, IntentDocument, IntentSpecialtySearch, IntentFindAProvider,
        )

        nucc_codes = payload.get("nucc_codes") or []
        if not isinstance(nucc_codes, list):
            return Response(
                kind="apply_filter",
                result={"ok": False, "error": "nucc_codes must be a list"},
            )
        # The user's Apply-Filter narrowing — list of code STRINGS the
        # FE sent. This stores on selected_nucc_codes and is what
        # ProviderSearch filters against. The full specialty universe
        # (the FE's panel) lives on nucc_codes and MUST NOT be
        # overwritten by Apply Filter — that's the bug Skip reported on
        # 2026-06-10: complaint did not change, so the filter choices
        # must not change either.
        selected_codes = [c for c in nucc_codes if isinstance(c, str)]
        selected_codes_json = json.dumps(selected_codes)

        prior = deps.user_object.intent
        complaint, geography = self._extract_complaint_and_geography(prior)

        if self._geography_sufficient(geography):
            new_doc = self._build_apply_filter_findaprovider_doc(
                prior, complaint, geography, selected_codes_json,
            )
            deps.user_object.intent = new_doc
            await self._dispatch_target_action(deps, new_doc, "findAProvider")
            return Response(
                kind="apply_filter",
                result={"target_action": "findAProvider"},
            )

        reason: dict[str, Any] = {
            "gesture": "apply_filter",
            "missing_slot": "geography",
            "selected_specialty_count": len(selected_codes),
        }
        if complaint:
            reason["prior_complaint"] = complaint
        partial_geo = {k: v for k, v in (geography or {}).items() if v}
        if partial_geo:
            reason["prior_partial_geography"] = partial_geo

        # Carry the user's selection through onto the existing
        # specialtySearch / findAProvider intents WITHOUT touching the
        # nucc_codes universe. The next-turn interpret path (after the
        # user supplies geography) will read selected_nucc_codes for
        # ProviderSearch.
        seeded_doc = self._seed_intent_with_selection(
            prior, complaint, geography, selected_codes_json,
        )
        deps.user_object.intent = seeded_doc

        from UtteranceManager import utterance_manager as utterance_manager_module
        try:
            await utterance_manager_module.TOOL.run_and_log(
                deps,
                utterance_manager_module.Request(
                    trigger_type="manufacture",
                    manufacture_utterance_reason=reason,
                ),
            )
        except ChatHealthyException as exc:
            if exc.mode != "llm_unavailable":
                raise
            log.exception(
                "UR caught ChatHealthyException — raised at server=%s "
                "component=%s; caught at server=shared_services "
                "component=UR; mode=%s message=%s context=%s",
                exc.server, exc.component, exc.mode, exc.message,
                exc.context,
                stack_info=True,
                exc=exc, if_not_debug_log=True,
            )
            await self._dispatch_llm_unavailable_dialogue(deps, exc)
            return Response(
                kind="apply_filter",
                result={"target_action": "closeConnection200",
                        "mode": "llm_unavailable"},
            )
        return Response(
            kind="apply_filter",
            result={"target_action": "closeConnection200"},
        )

    async def _handle_about_chathealthy(self, deps: AgentDeps, payload: dict[str, Any]) -> Response:
        """Thin pass-through to AboutChatHealthyTool. Tool streams
        `kind:about_chathealthy` with structured data; React widget
        renders HTML and calls ClientRouter.render."""
        from AboutChatHealthy import about_chathealthy_tool
        req = about_chathealthy_tool.Request(**(payload or {}))
        await about_chathealthy_tool.TOOL.run_and_log(deps, req)
        return Response(kind="about_chathealthy", result={"ok": True})

    async def _handle_claim_oauth_result(self, deps: AgentDeps, payload: dict[str, Any]) -> Response:
        """Pop the OAuth result the callback stashed on user_object.
        Returns {result: {...}} for the React HeaderWidget to render its
        banner, then clears the field so the next poll returns nothing.
        Pure structured-data path: no HTML anywhere.

        Atomic find_one_and_update: claim and clear in one Mongo round-trip
        so a concurrent /gate save with a snapshot of the user_object from
        before the OAuth-callback persist cannot overwrite the persisted
        result. The user_object's in-memory pending_oauth_result is also
        cleared so the request-end save (write_session_record) doesn't put
        it back.
        """
        from pymongo import ReturnDocument
        from authentication.authorizations_and_authentications_tool import (
            SESSION_DB, SESSION_COLLECTION,
        )
        user_obj = deps.user_object
        guid = user_obj.current_session_token.get_auth_token()
        coll = deps.mongo_frontend[SESSION_DB][SESSION_COLLECTION]
        before = coll.find_one_and_update(
            {"_id": guid, "pending_oauth_result": {"$ne": None}},
            {"$set": {"pending_oauth_result": None}},
            projection={"pending_oauth_result": 1},
            return_document=ReturnDocument.BEFORE,
        )
        result = before.get("pending_oauth_result") if before else None
        user_obj.pending_oauth_result = None
        data = {"result": result}
        deps.stream({"kind": "oauth_result", "data": data})
        return Response(kind="claim_oauth_result", result=data)

    async def _handle_provider_selection(self, deps: AgentDeps, payload: dict[str, Any]) -> Response:
        """Thin pass-through to ProviderSelectionTool. Tool mutates
        user_object.selected_providers and streams `kind:selection_changed`
        with the current NPI list; SelectedProvidersWidget repaints its
        strip via ClientRouter.merge into MainWindow's selected_strip
        region."""
        from ProviderSelection import provider_selection_tool
        req = provider_selection_tool.Request(**(payload or {}))
        await provider_selection_tool.TOOL.run_and_log(deps, req)
        return Response(kind="provider_selection", result={"ok": True})

    async def _handle_clinical_trial_selection(self, deps: AgentDeps, payload: dict[str, Any]) -> Response:
        """Thin pass-through to ClinicalTrialSelectionTool. Tool mutates
        user_object.selected_clinical_trials and streams
        `kind:trial_selection_changed` with the current NCT-ID list; the
        SelectedClinicalTrialsWidget repaints its strip via ClientRouter.merge
        into MainWindow's selected_trials_strip region."""
        from ClinicalTrialSelection import clinical_trial_selection_tool
        req = clinical_trial_selection_tool.Request(**(payload or {}))
        await clinical_trial_selection_tool.TOOL.run_and_log(deps, req)
        return Response(kind="clinical_trial_selection", result={"ok": True})

    # ── Orchestration helpers ─────────────────────────────────────

    @staticmethod
    def _geography_sufficient(geo: Optional[dict]) -> bool:
        """Same rule findAProvider applies — zip OR state. city/county
        without state are not enough."""
        if not geo:
            return False
        return bool((geo.get("zip") or "").strip()) or bool((geo.get("state") or "").strip())

    @staticmethod
    def _extract_complaint_and_geography(
        document,
    ) -> tuple[str, dict]:
        """Read the latest known complaint + geography off the carried
        IntentDocument. Looks at findAProvider first (preferred — carries
        both), falls back to specialtySearch for complaint. Returns
        ('', {}) if no useful data is found."""
        import json as json
        if document is None:
            return "", {}
        complaint = ""
        geography: dict = {}
        for name in ("findAProvider", "specialtySearch"):
            entry = next((i for i in document.intents if i.name == name), None)
            if entry is None:
                continue
            for arg in entry.arguments:
                if arg.name == "complaint" and arg.value and not complaint:
                    complaint = arg.value
                if arg.name == "geography" and arg.value and not geography:
                    try:
                        parsed = json.loads(arg.value)
                        if isinstance(parsed, dict):
                            geography = parsed
                    except json.JSONDecodeError as _exc:
                        # Mode 1 (REQ-B-008): geography arg JSON malformed;
                        # UR skips and continues. log.info + default debug-gated.
                        log.info("geography arg JSON decode failed (skipped): %s", _exc, exc=ChatHealthyException(
                                                                                             mode="geography_arg_json_decode_failed",
                                                                                             message=f"geography arg JSON decode failed (skipped): {_exc}",
                                                                                             component="UniversalNavigationTool",
                                                                                             exception=_exc,
                                                                                         ), if_not_debug_log=True)
        return complaint, geography

    @staticmethod
    def _carry_universe_args(prior_entry) -> list:
        """Pull complaint + nucc_codes (universe) + geography from an
        existing intent entry on the prior document. Returns a list of
        Argument objects ready to be re-emitted on a refreshed entry.
        Filters out any prior selected_nucc_codes — Apply Filter
        regenerates that on every click."""
        from chathealthy_lib.authentication.intent_document import Argument
        out: list = []
        if prior_entry is None:
            return out
        for arg in prior_entry.arguments:
            if arg.name == "selected_nucc_codes":
                continue
            out.append(Argument(
                name=arg.name, value=arg.value, type=arg.type, required=arg.required,
            ))
        return out

    @staticmethod
    def _build_apply_filter_findaprovider_doc(
        prior, complaint: str, geography: dict, selected_codes_json: str,
    ):
        """When apply_filter arrives with sufficient geography, build a
        synthetic IntentDocument with target_action=findAProvider. The
        existing dispatch path runs ProviderSearch with the user's
        selection (selected_nucc_codes). The full specialty universe
        carried on nucc_codes is preserved — it is what the FE filter
        panel renders, and the complaint did not change so the universe
        does not change."""
        import json as json
        from chathealthy_lib.authentication.intent_document import (
            Argument, IntentDocument, IntentFindAProvider, IntentSpecialtySearch,
        )
        prior_spec = next(
            (i for i in (prior.intents if prior is not None else []) if i.name == "specialtySearch"),
            None,
        ) if prior is not None else None
        prior_findap = next(
            (i for i in (prior.intents if prior is not None else []) if i.name == "findAProvider"),
            None,
        ) if prior is not None else None

        # specialtySearch: carry everything except selected_nucc_codes
        # from prior + add the new selected_nucc_codes. Ensure complaint
        # is present.
        spec_args = UniversalNavigationTool._carry_universe_args(prior_spec)
        if not any(a.name == "complaint" for a in spec_args):
            spec_args.insert(0, Argument(
                name="complaint", value=complaint or "", type="string", required=True,
            ))
        spec_args.append(Argument(
            name="selected_nucc_codes", value=selected_codes_json,
            type="array", required=False,
        ))
        spec = IntentSpecialtySearch(name="specialtySearch", arguments=spec_args)

        # findAProvider: carry everything (universe nucc_codes,
        # complaint, prior geography) + new geography from THIS turn +
        # new selected_nucc_codes.
        geo_compact = {k: v for k, v in (geography or {}).items() if v}
        findap_args = UniversalNavigationTool._carry_universe_args(prior_findap)
        findap_args = [a for a in findap_args if a.name not in ("complaint", "geography")]
        findap_args.insert(0, Argument(
            name="complaint", value=complaint or "", type="string", required=True,
        ))
        findap_args.append(Argument(
            name="geography", value=json.dumps(geo_compact),
            type="object", required=True,
        ))
        findap_args.append(Argument(
            name="selected_nucc_codes", value=selected_codes_json,
            type="array", required=False,
        ))
        findap = IntentFindAProvider(name="findAProvider", arguments=findap_args)

        intents: list = [spec, findap]
        if prior is not None:
            for entry in prior.intents:
                if entry.name not in ("specialtySearch", "findAProvider"):
                    intents.append(entry)
        return IntentDocument(
            target_action="findAProvider",
            intents=intents[:4],
            user_message=None,
        )

    @staticmethod
    def _seed_intent_with_selection(
        prior, complaint: str, geography: dict, selected_codes_json: str,
    ):
        """Apply-Filter with insufficient geography: write the user's
        selection onto selected_nucc_codes on the carried specialtySearch
        and findAProvider intents WITHOUT touching the nucc_codes
        universe. The complaint did not change, so the universe does not
        change. The next-turn interpret path will see selected_nucc_codes
        and ProviderSearch will use it as its filter."""
        import json as json
        from chathealthy_lib.authentication.intent_document import (
            Argument, IntentDocument, IntentFindAProvider, IntentSpecialtySearch,
        )
        prior_spec = next(
            (i for i in (prior.intents if prior is not None else []) if i.name == "specialtySearch"),
            None,
        ) if prior is not None else None
        prior_findap = next(
            (i for i in (prior.intents if prior is not None else []) if i.name == "findAProvider"),
            None,
        ) if prior is not None else None

        # specialtySearch: carry everything (universe nucc_codes,
        # complaint) + new selected_nucc_codes.
        spec_args = UniversalNavigationTool._carry_universe_args(prior_spec)
        if not any(a.name == "complaint" for a in spec_args):
            spec_args.insert(0, Argument(
                name="complaint", value=complaint or "", type="string", required=True,
            ))
        spec_args.append(Argument(
            name="selected_nucc_codes", value=selected_codes_json,
            type="array", required=False,
        ))
        spec = IntentSpecialtySearch(name="specialtySearch", arguments=spec_args)

        intents: list = [spec]

        # findAProvider only if we have prior partial geography to carry.
        geo_compact = {k: v for k, v in (geography or {}).items() if v}
        if geo_compact or prior_findap is not None:
            findap_args = UniversalNavigationTool._carry_universe_args(prior_findap)
            findap_args = [a for a in findap_args if a.name not in ("complaint", "geography")]
            findap_args.insert(0, Argument(
                name="complaint", value=complaint or "", type="string", required=True,
            ))
            if geo_compact:
                findap_args.append(Argument(
                    name="geography", value=json.dumps(geo_compact),
                    type="object", required=True,
                ))
            findap_args.append(Argument(
                name="selected_nucc_codes", value=selected_codes_json,
                type="array", required=False,
            ))
            intents.append(IntentFindAProvider(name="findAProvider", arguments=findap_args))

        if prior is not None:
            for entry in prior.intents:
                if entry.name not in ("specialtySearch", "findAProvider"):
                    intents.append(entry)
        return IntentDocument(
            target_action="specialtySearch",
            intents=intents[:4],
            user_message=None,
        )

    async def _hydrate_lockout_if_any(self, deps: AgentDeps) -> None:
        """UR's pre-UM hydration step for the safetyLockout flow.

        Single find_one against {env}_Safety.emergency_incidents keyed by
        user_object.ip_address. If an active record exists (expires_at >
        now AND unlocked != true), stamps is_locked_out=True and a Lockout
        sub-object straight from the DB record's matching fields. Otherwise
        leaves user_object untouched.

        All three LockoutTool tasks read their inputs from user_object;
        this is the only DB read in the lockout flow.
        """
        ip = (deps.user_object.ip_address or "").strip()
        if not ip:
            return
        if deps.mongo_frontend is None:
            return
        coll = deps.mongo_frontend[f"{ENV}_Safety"]["emergency_incidents"]
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            record = coll.find_one({
                "ip": ip,
                "expires_at": {"$gt": now_iso},
                "unlocked": {"$ne": True},
            })
        except Exception as exc:
            # Mode 2 (REQ-B-008): Mongo lockout-table query failed; UR
            # proceeds as if not locked (operator must know). log.error +
            # if_not_debug_log=True.
            log.error("UR: _hydrate_lockout_if_any find_one failed: %s", exc, exc=ChatHealthyException(
                                                                                   mode="ur_hydrate_lockout_find_failed",
                                                                                   message=f"UR: _hydrate_lockout_if_any find_one failed: {exc}",
                                                                                   component="UniversalNavigationTool",
                                                                                   exception=exc,
                                                                               ), if_not_debug_log=True)
            return
        if record is None:
            return
        from chathealthy_lib.authentication.user_object import Lockout
        expires_str = record.get("expires_at") or ""
        try:
            expires_at = datetime.fromisoformat(expires_str)
        except Exception as _exc:
            # Mode 1 (REQ-B-008): bad data in emergency_incidents row;
            # treated as inactive and UR continues. log.info + default
            # debug-gated.
            log.info(
                "UR: emergency_incidents row for ip=%s has invalid expires_at=%r; "
                "treating as inactive", ip[:16] + "...", expires_str,
                exc=ChatHealthyException(
                 mode="ur_lockout_expires_at_invalid",
                 message=f"UR: emergency_incidents row for ip={ip[:16]}... has invalid expires_at={expires_str!r}; treating as inactive",
                 component="UniversalNavigationTool",
                 exception=_exc,
             ), if_not_debug_log=True,
            )
            return
        deps.user_object.is_locked_out = True
        deps.user_object.lockout = Lockout(
            expires_at=expires_at,
            trigger_utterance=str(record.get("trigger_message") or ""),
            history=list(record.get("history") or []),
        )
        log.debug(
            "UR: hydrated lockout for ip=%s; expires_at=%s",
            ip[:16] + "...", expires_str,
        )

    def _validate_document(self, document, target_action: str) -> None:
        """UR compliance check on the IntentDocument: target_action enumerated
        by Pydantic; must correspond to a name in intents[]; required arguments
        non-empty and parseable; findAProvider geography sufficiency."""
        import json as json

        target_intent_entry = next(
            (i for i in document.intents if i.name == target_action), None,
        )
        if target_intent_entry is None:
            raise ChatHealthyException(
            mode="runtime_error",
            component="universal_navigation_tool",
            message=f"UR compliance: target_action {target_action!r} has no matching "
                f"entry in intents[] (names={[i.name for i in document.intents]})")
        for arg in target_intent_entry.arguments:
            if arg.required and not arg.value:
                raise ChatHealthyException(
            mode="runtime_error",
            component="universal_navigation_tool",
            message=f"UR compliance: required argument {arg.name!r} for target_action "
                    f"{target_action!r} has empty value")
            if arg.type == "boolean" and arg.value not in ("true", "false"):
                raise ChatHealthyException(
            mode="runtime_error",
            component="universal_navigation_tool",
            message=f"UR compliance: boolean argument {arg.name!r} has value "
                    f"{arg.value!r}, must be 'true' or 'false'")
            if arg.type in ("object", "array"):
                try:
                    json.loads(arg.value)
                except json.JSONDecodeError as exc:
                    raise ChatHealthyException(
                        mode="ur_compliance_arg_invalid_json",
                        message=f"UR compliance: {arg.type} argument {arg.name!r} value is not valid JSON: {exc}",
                        component="UniversalNavigationTool",
                        exception=exc,
                    )

        if target_action == "findAProvider":
            geo_arg = next(
                (a for a in target_intent_entry.arguments if a.name == "geography"), None,
            )
            if geo_arg is None:
                raise ChatHealthyException(
            mode="runtime_error",
            component="universal_navigation_tool",
            message="UR compliance: findAProvider missing geography argument")
            geo = json.loads(geo_arg.value)
            zip_code = (geo.get("zip") or "").strip()
            state = (geo.get("state") or "").strip()
            if not zip_code and not state:
                raise ChatHealthyException(
            mode="runtime_error",
            component="universal_navigation_tool",
            message="UR compliance: findAProvider geography insufficient — needs "
                    "zip OR state (city/county without state are not enough)")

    async def _dispatch_llm_unavailable_dialogue(
        self, deps: AgentDeps, exc: ChatHealthyException,
    ) -> None:
        """Rung 2 of the failure ladder (EPIC-008-F-002-S-009-REQ-B-007).

        UM's classifier or apply_filter LLM call exhausted retries against
        an LLM provider. Re-dispatch UM with a manufacture trigger so it
        authors a non-technical user-facing message via the existing
        manufacture pattern. If THIS manufacture call also raises
        ChatHealthyException, it propagates to UR's outermost catch and
        the existing fatal path fires (rung 3).
        """
        from UtteranceManager import utterance_manager as utterance_manager_module
        reason = {
            "gesture": "system_llm_unavailable",
            "raised_at_server": exc.server,
            "raised_at_component": exc.component,
            "caught_at_server": "shared_services",
            "caught_at_component": "UR",
            "provider": exc.context.get("provider"),
            "call_site": exc.context.get("call_site"),
            "attempts": exc.context.get("attempts"),
        }
        await utterance_manager_module.TOOL.run_and_log(
            deps,
            utterance_manager_module.Request(
                trigger_type="manufacture",
                manufacture_utterance_reason=reason,
            ),
        )

    async def _dispatch_target_action(self, deps: AgentDeps, document, target_action: str) -> None:
        """Dispatch the tool that owns this target_action. Tools may mutate
        user_object.intent before returning; the caller loops and re-dispatches."""
        import json as json

        target_intent_entry = next(
            (i for i in document.intents if i.name == target_action), None,
        )

        if target_action == "nonsense":
            from NonsenseTool import nonsense_tool
            await nonsense_tool.TOOL.run_and_log(deps, nonsense_tool.Request())

        elif target_action == "safetyLockout":
            # UM judged the latest utterance signals immediate medical
            # attention. LockoutTool's Task C inserts the
            # {env}_Safety.emergency_incidents record, stamps user_object
            # state, streams the deterministic "why" + Skip phone number,
            # and morphs target_action to closeConnection200.
            from LockoutTool import lockout_tool
            await lockout_tool.TOOL.run_and_log(deps, lockout_tool.Request())

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
            await self._run_or_cache_specialty_filter(deps, complaint)
            # No inner "final" emission — _run_pipeline_then_finalize
            # emits the single canonical final event with full payload.

        elif target_action == "findClinicalTrials":
            # EPIC-006-F-031 — dispatch to FindCare backend's
            # /clinical_trials endpoint via the SS-side dispatcher.
            # The clinical-trials tool itself lives in FindCare; SS
            # carries only the cross-service forwarder.
            from authentication import clinical_trials_dispatcher
            complaint = next(
                (a.value for a in target_intent_entry.arguments if a.name == "complaint"),
                "",
            )
            user_location = next(
                (a.value for a in target_intent_entry.arguments if a.name == "user_location"),
                None,
            )
            age_years_raw = next(
                (a.value for a in target_intent_entry.arguments if a.name == "age_years"),
                None,
            )
            try:
                age_years = int(age_years_raw) if age_years_raw is not None else None
            except (TypeError, ValueError):
                age_years = None
            sex_filter = next(
                (a.value for a in target_intent_entry.arguments if a.name == "sex"),
                None,
            )
            geographic_scope = next(
                (a.value for a in target_intent_entry.arguments if a.name == "geographic_scope"),
                None,
            )
            # Emit the canonical criteria the moment classification is done
            # so the client's searching banner can replace the raw utterance
            # with what the system is actually about to fetch.
            deps.stream({
                "kind": "intent_classified",
                "data": {
                    "action": "findClinicalTrials",
                    "condition": complaint,
                    "user_location": user_location,
                    "age_years": age_years,
                    "sex": sex_filter,
                    "geographic_scope": geographic_scope,
                },
            })
            ct_req = clinical_trials_dispatcher.Request(
                condition=complaint,
                user_location=user_location,
                age_years=age_years,
                sex=sex_filter,
                geographic_scope=geographic_scope,
            )
            await clinical_trials_dispatcher.TOOL.run_and_log(deps, ct_req)

        elif target_action == "findAProvider":
            from authentication import provider_search_and_selection_tool

            complaint = next(
                (a.value for a in target_intent_entry.arguments if a.name == "complaint"),
                "",
            )
            specialties = await self._run_or_cache_specialty_filter(deps, complaint)
            if specialties:
                # FindCare-UR REQ-B-002: ProviderSearch fires only when
                # geography is sufficient.
                geo_arg_val = next(
                    (a for a in target_intent_entry.arguments if a.name == "geography"),
                    None,
                )
                geo = json.loads(geo_arg_val.value) if geo_arg_val else {}

                # Apply-Filter selection (if any) narrows the search.
                # Absent selection: search the full universe.
                selected_arg = next(
                    (a for a in target_intent_entry.arguments if a.name == "selected_nucc_codes"),
                    None,
                )
                if selected_arg and selected_arg.value:
                    try:
                        selected_codes = json.loads(selected_arg.value)
                    except json.JSONDecodeError as _exc:
                        # Mode 1 (REQ-B-008): malformed JSON defaults to [];
                        # UR continues. log.info + default debug-gated.
                        selected_codes = []
                    if isinstance(selected_codes, list) and selected_codes:
                        specialty_codes = [c for c in selected_codes if isinstance(c, str)]
                    else:
                        specialty_codes = [c["code"] for c in specialties]
                else:
                    specialty_codes = [c["code"] for c in specialties]

                ps_req = provider_search_and_selection_tool.Request(
                    specialty_codes=specialty_codes,
                    state=geo.get("state"),
                    city=geo.get("city"),
                    county=geo.get("county"),
                    zip=geo.get("zip"),
                    limit=25,
                )
                await provider_search_and_selection_tool.TOOL.run_and_log(
                    deps, ps_req,
                )
                # No inner "final" emission — outer pipeline emits the
                # canonical final event with full payload.

        else:
            raise ChatHealthyException(
            mode="runtime_error",
            component="universal_navigation_tool",
            message=f"UR compliance: out-of-catalog target_action {target_action!r}")

    async def _run_or_cache_specialty_filter(self, deps: AgentDeps, complaint: str) -> list[dict]:
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
        import json as json
        from chathealthy_lib.authentication.intent_document import Argument

        document = deps.user_object.intent
        if document is None:
            return []

        # Caching nucc_codes across utterances is a bug, not an
        # optimization. The original cache key was the IntentDocument
        # (not the complaint), so a prior dental query's nucc_codes would
        # be returned verbatim for a new orthopedic query and the FE
        # would show dental specialties for "find me a bone doctor"
        # (operator-reported 2026-06-21). SpecialtyFilter is cheap; the
        # right behavior is to run it on every new utterance.
        # read_nucc_codes_cache() is retained for code that legitimately
        # needs the prior turn's codes (e.g., ProviderSearch fallback
        # when Apply Filter sends an empty selection), but it is NOT
        # called here.

        # Restore prior behavior: SpecialtyFilter sees the VERBATIM latest
        # person utterance, not UM's narrow `complaint` extraction. The
        # filter's prompt scores user-named roles ("nurse practitioner",
        # "acupuncturist", etc.) at 1.0 with adjacents <=0.7 — but only if
        # the role name actually reaches it. UM's complaint extraction
        # strips role names, so the filter must source the raw utterance.
        raw_utterance = ""
        for u in reversed(deps.user_object.session_conversation_history.utterances):
            actor = getattr(u, "actor", None) or (u.get("actor") if isinstance(u, dict) else None)
            text = getattr(u, "text", None) or (u.get("text") if isinstance(u, dict) else "")
            if actor == "person" and text:
                raw_utterance = str(text).strip()
                break
        query = raw_utterance or complaint
        from SpecialtyFilter import specialty_filter_tool
        fs = await specialty_filter_tool.TOOL.run_and_log(
            deps, specialty_filter_tool.Request(query=query),
        )
        if fs.error or not fs.specialties:
            return []

        specialties = [s.model_dump(exclude_none=True) for s in fs.specialties]
        encoded = json.dumps(specialties)

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
        from chathealthy_lib.authentication.intent_document import IntentDocument as _IntentDocument
        deps.user_object.intent = _IntentDocument(
            target_action=document.target_action,
            intents=new_intents,
            user_message=document.user_message,
        )

        return specialties

    # ── /gate orchestration entry point ───────────────────────────

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
        if gate_req.intent is not None and gate_req.intent not in KNOWN_WIRE_INTENTS:
            raise ChatHealthyException(
            mode="value_error",
            component="universal_navigation_tool",
            message=f"/gate: unknown intent {gate_req.intent!r}; expected one of "
                f"{sorted(KNOWN_WIRE_INTENTS)} or absent")

        # 2. Mongo handle + AuthnDeps.
        mongo_frontend = authn.get_mongo_frontend()
        authn_deps = AuthnDeps(
            session_guid=gate_req.session_guid,
            server_env=ENV,
            mongo_frontend=mongo_frontend,
        )

        # 3. Session load + auth_intent decision.
        loaded_user_object: Optional[UserObject] = None
        if gate_req.session_guid:
            sessions_coll = mongo_frontend[authn.SESSION_DB][authn.SESSION_COLLECTION]
            session_doc = sessions_coll.find_one({"_id": gate_req.session_guid})
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
                    # Mode 2 (REQ-B-008): persisted session doc can't be
                    # deserialized into a UserObject — user loses their
                    # state and gets fresh-minted as guest on this turn.
                    # User-affecting → log.error + always-log.
                    pass

        if loaded_user_object is not None:
            auth_intent = "manage_session"
            inbound_user_object = loaded_user_object
            sch_dump = loaded_user_object.session_conversation_history.model_dump()
            intent_dump = (
                loaded_user_object.intent.model_dump()
                if loaded_user_object.intent is not None else None
            )
        else:
            auth_intent = "manufacture_session"
            inbound_user_object = UserObject(
                current_session_token="NULL",
                expires_at=datetime.now(timezone.utc)
                + timedelta(seconds=SESSION_TTL_SECONDS),
            )

        # 4. Call AUTHN_TOOL.run.
        authn_resp = await authn.TOOL.run(
            authn_deps,
            authn.Request(intent=auth_intent, user_object=inbound_user_object),
        )
        user_object = authn_resp.user_object
        fresh_mint = authn_resp.fresh_mint
        guid = user_object.current_session_token.get_auth_token()

        # Bind the user_object's GUID to the logging context for this
        # async task. Every log emitted from here on (handle_gate +
        # downstream tools + middleware request-end log) carries
        # session_guid + user_action=true automatically. The developer
        # never harvests the GUID.
        from chathealthy_lib.logging_service import bind_user_object_to_log
        bind_user_object_to_log(user_object)

        # Stamp client IP onto user_object at the HTTP boundary so tools
        # (SafetyLockoutTool in particular) never touch the Request. UR's
        # subsequent _hydrate_lockout_if_any consumes this in
        # _handle_utterance.
        if gate_req.client_ip:
            user_object.ip_address = gate_req.client_ip

        # 5. (login_register short-circuit removed — that flow now goes
        #    direct to /auth/google/start as a form-target popup post
        #    which routes through OAuthLoginTool start phase. /gate's
        #    only client-facing intent is utterance traffic.)

        # 6. Build event queue + stream sink + AgentDeps + nav.Request.
        event_queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        def stream_sink(event: dict) -> None:
            event_queue.put_nowait(event)

        agent_deps = AgentDeps(
            user_object=user_object,
            session_token=user_object.current_session_token,
            mongo_frontend=mongo_frontend,
            server_env=ENV,
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
                    # Mode 2 (REQ-B-008): UR's broad Exception catch surfaces
                    # the failure to the user via the final ok:false stream
                    # event (NOT a 503). Per the taxonomy, only the catch-
                    # all @app.exception_handler does Mode 3 fatal_error.
                    # This catch needs follow-on work: discriminate on
                    # exc.mode for known ChatHealthyException modes (Mode 1
                    # or Mode 2 per mode); for non-ChatHealthyException,
                    # re-raise so the safety net handles it as Mode 3.

                session_token_proj = session_token_wire(user_object)
                if nav_exc_local is not None:
                    final_event_local = {
                        "kind": "final", "ok": False,
                        "error": f"{type(nav_exc_local).__name__}: {nav_exc_local}",
                        "guid": guid,
                        "time_remaining_seconds": time_remaining_seconds(user_object),
                        "was_registered": was_registered(user_object),
                        "session_token": session_token_proj,
                    }
                else:
                    final_event_local = {
                        "kind": "final", "ok": True,
                        "guid": guid,
                        "result": res_local.result if res_local else {},
                        "result_kind": res_local.kind if res_local else "unknown",
                        "time_remaining_seconds": time_remaining_seconds(user_object),
                        "was_registered": was_registered(user_object),
                        "session_token": session_token_proj,
                    }

                try:
                    sch_dump = user_object.session_conversation_history.model_dump()
                    intent_dump = (
                        user_object.intent.model_dump()
                        if user_object.intent is not None else None
                    )
                    await authn.TOOL.persist(authn_deps, user_object, fresh_mint)
                except Exception as exc:
                    # Mode 2 (REQ-B-008): session persist to Mongo failed.
                    # Stream continues so the user sees this turn's result,
                    # but next turn will rehydrate stale state. Operator
                    # must know. log.exception (ERROR+traceback) + always-log.
                    pass

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
                    yield (json.dumps(item, default=str) + "\n").encode("utf-8")

            return GateResponse(
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
            body_kind="json",
            body_data=final_event or {},
        )


TOOL = UniversalNavigationTool()
