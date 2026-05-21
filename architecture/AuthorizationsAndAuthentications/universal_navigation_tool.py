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
NavResult carrying the final event + any history_append directive so the
gate route can $push into admin.sessions after the run.

Canonical *_tool.py exports: TOOL_NAME, Request, Response, run().
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from authentication.agent_deps import (
    AgentDeps,
    log_utterance,
    log_ux_event,
)
from authentication.chathealthy_tool import ChatHealthyTool
from authentication import provider_search_and_selection_tool
from SpecialtyFilter import specialty_filter_tool

_log = logging.getLogger("shared_services.universal_navigation")

TOOL_NAME = "universal_navigation"


# ────────────────────────────────────────────────────────────────────
# Request / Response contracts
# ────────────────────────────────────────────────────────────────────

class Request(BaseModel):
    """Op + opaque payload. The router picks a handler by `op`."""
    op: str = Field(default="boot")
    payload: dict[str, Any] = Field(default_factory=dict)


class HistoryAppend(BaseModel):
    """A directive the gate executes after the navigation run: $push the
    given entry onto the named session_conversation_history.<array>."""
    array: Literal["ux_events", "utterances"]
    entry: dict[str, Any]


class Response(BaseModel):
    kind: str
    result: dict[str, Any] = Field(default_factory=dict)
    history_append: Optional[HistoryAppend] = None


# ────────────────────────────────────────────────────────────────────
# History helpers (absorbed from the deleted session_conversation_history.py)
# ────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ux_event(event_type: str, value: Any, pedantic_response: Any) -> HistoryAppend:
    entry: dict[str, Any] = {
        "event_type": event_type,
        "value": value,
        "at": _now_iso(),
    }
    if pedantic_response is not None:
        entry["pedantic_response"] = (
            pedantic_response if isinstance(pedantic_response, dict)
            else {"text": str(pedantic_response)}
        )
    return HistoryAppend(array="ux_events", entry=entry)


def _utterance(text: str, bridge_response: Optional[dict[str, Any]] = None) -> HistoryAppend:
    entry: dict[str, Any] = {"text": text, "at": _now_iso()}
    if bridge_response is not None:
        entry["bridge_response"] = bridge_response
    return HistoryAppend(array="utterances", entry=entry)


_SPLASH_PEDANTIC = "SharedServices took ownership of the page and rendered the User Object."


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
    if not isinstance(sch, dict):
        sch = {}
    ux_events = sch.get("ux_events") if isinstance(sch.get("ux_events"), list) else []
    utterances = sch.get("utterances") if isinstance(sch.get("utterances"), list) else []
    threads = {
        "empty": not ux_events and not utterances,
        "person": _collect_person(utterances),
        "person_to_system": _collect_person_to_system(ux_events),
        "machine": _collect_machine(ux_events, utterances),
        "llm_to_system": _collect_llm_to_system(utterances),
    }
    return {
        "identity": identity,
        "threads": threads,
    }


def _collect_person(utterances: list) -> list[dict[str, Any]]:
    out = [
        {"at": str(u.get("at", "")), "text": str(u.get("text", ""))}
        for u in utterances if isinstance(u, dict)
    ]
    out.sort(key=lambda r: r["at"])
    return out


def _collect_person_to_system(ux_events: list) -> list[dict[str, Any]]:
    out = [
        {
            "at": str(e.get("at", "")),
            "event_type": str(e.get("event_type", "?")),
            "value": e.get("value"),
        }
        for e in ux_events if isinstance(e, dict)
    ]
    out.sort(key=lambda r: r["at"])
    return out


def _collect_machine(ux_events: list, utterances: list) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in ux_events:
        if not isinstance(e, dict):
            continue
        ped = e.get("pedantic_response")
        if not ped:
            continue
        text = ped.get("text") if isinstance(ped, dict) else str(ped)
        out.append({
            "at": str(e.get("at", "")),
            "kind": "pedantic",
            "text": str(text or ""),
        })
    for u in utterances:
        if not isinstance(u, dict):
            continue
        br = u.get("bridge_response") or {}
        if isinstance(br, dict) and br.get("kind") == "tool_invocation":
            out.append({
                "at": str(u.get("at", "")),
                "kind": "tool_result",
                "tool_name": str(br.get("tool_name", "?")),
                "tool_result": br.get("tool_result"),
            })
    out.sort(key=lambda r: r["at"])
    return out


def _collect_llm_to_system(utterances: list) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for u in utterances:
        if not isinstance(u, dict):
            continue
        br = u.get("bridge_response") or {}
        if not isinstance(br, dict):
            continue
        kind = br.get("kind")
        at = str(u.get("at", ""))
        if kind == "llm_clarification":
            out.append({
                "at": at,
                "kind": "llm_clarification",
                "llm_response": str(br.get("llm_response", "")),
                "info_sought": br.get("info_sought") or [],
            })
        elif kind == "tool_invocation":
            out.append({
                "at": at,
                "kind": "tool_invocation",
                "tool_name": str(br.get("tool_name", "?")),
                "tool_args": br.get("tool_args"),
            })
    out.sort(key=lambda r: r["at"])
    return out


# ────────────────────────────────────────────────────────────────────
# Op handlers — graph nodes
# ────────────────────────────────────────────────────────────────────

async def _handle_boot(deps: AgentDeps, payload: dict[str, Any]) -> Response:
    deps.stream({"kind": "boot", "data": {"ok": True}})
    return Response(kind="boot", result={"op": "boot"})


async def _handle_splash(deps: AgentDeps, payload: dict[str, Any]) -> Response:
    data = _splash_data(deps.user_object)
    log_ux_event(
        deps.user_object,
        "splash_displayed",
        value=None,
        pedantic_response={"text": _SPLASH_PEDANTIC},
    )
    deps.stream({"kind": "splash", "data": data})
    return Response(kind="splash", result=data)


async def _handle_record_ux_event(deps: AgentDeps, payload: dict[str, Any]) -> Response:
    event_type = str(payload.get("event_type") or "").strip()
    if not event_type:
        return Response(kind="record_ux_event", result={"ok": False, "error": "event_type required"})
    log_ux_event(
        deps.user_object,
        event_type,
        value=payload.get("value"),
        pedantic_response=payload.get("pedantic_response"),
    )
    deps.stream({"kind": "ux_event_recorded", "data": {"event_type": event_type}})
    return Response(kind="record_ux_event", result={"ok": True})


async def _handle_utterance(deps: AgentDeps, payload: dict[str, Any]) -> Response:
    """Person types something. Log it (Person stream). Invoke delegate
    tools via their `run_and_log()` so the LLM → System and Machine
    streams pick up the invocation automatically. Tools never log
    themselves."""
    text = str(payload.get("text") or "").strip()
    if not text:
        return Response(kind="utterance", result={"ok": False, "error": "text required"})

    # Person stream — what the user typed.
    log_utterance(deps.user_object, text)

    # Step 1 — SpecialtyFilter delegate (auto-logs via base class).
    fs_req = specialty_filter_tool.Request()
    fs = await specialty_filter_tool.TOOL.run_and_log(deps, fs_req)

    if fs.error or not fs.specialties:
        deps.stream({"kind": "final", "data": {"ok": False, "error": fs.error or "no_specialties"}})
        return Response(
            kind="utterance",
            result={"ok": False, "classify": fs.model_dump(exclude_none=True)},
        )

    # Step 2 — ProviderSearchAndSelection delegate (auto-logs).
    codes = [s.code for s in fs.specialties]
    ps_req = provider_search_and_selection_tool.Request(
        specialty_codes=codes,
        state=fs.state,
        city=fs.city,
        county=fs.county,
        limit=25,
    )
    ps = await provider_search_and_selection_tool.TOOL.run_and_log(deps, ps_req)

    deps.stream({"kind": "final", "data": {"ok": True}})
    return Response(
        kind="utterance",
        result={
            "ok": True,
            "classify": fs.model_dump(exclude_none=True),
            "search": ps.model_dump(exclude_none=True),
        },
    )


_HANDLERS = {
    "boot": _handle_boot,
    "splash": _handle_splash,
    "record_ux_event": _handle_record_ux_event,
    "utterance": _handle_utterance,
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


TOOL = UniversalNavigationTool()
