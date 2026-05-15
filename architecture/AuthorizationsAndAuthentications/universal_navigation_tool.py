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

import html as _html
import json as _json
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
from authentication import specialty_filter_tool, provider_search_and_selection_tool

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


# ────────────────────────────────────────────────────────────────────
# Splash render (absorbed from the deleted splash_endpoint.py)
# ────────────────────────────────────────────────────────────────────

_SPLASH_PEDANTIC = "SharedServices took ownership of the page and rendered the User Object."


def _splash_html(user_object) -> str:
    cst = user_object.current_session_token
    identity = {
        "user_type": "Guest",
        "guid": cst.get_auth_token(),
        "origin": cst.origin,
        "server_env": cst.server_env,
        "created_at": cst.created_at,
        "expires_at": user_object.expires_at,
    }
    sch = user_object.session_conversation_history.model_dump()
    return _splash_render(identity, sch)


def _splash_render(identity: dict, sch: dict | None) -> str:
    if not isinstance(sch, dict):
        sch = {}
    return (
        '<div style="padding:20px;max-width:820px;margin:0 auto;text-align:left;'
        'font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;">'
        '<h2 style="margin:0 0 8px 0;color:#0b7a75;font-size:22px;">'
        'Shared Services &mdash; User Object'
        '</h2>'
        '<p style="color:#6b7280;margin:0 0 12px 0;font-size:13px;">'
        'Live evidence that the entrance code completed. The cookie carries only the GUID; '
        'the user object lives in <code>admin.Sessions</code>.'
        '</p>'
        + _splash_identity(identity)
        + _splash_history(sch)
        + '</div>'
    )


def _splash_identity(display: dict) -> str:
    rows: list[str] = []
    for key in ("user_type", "guid", "origin", "server_env", "created_at", "expires_at"):
        if key in display:
            rows.append(_splash_kv_row(key, display[key]))
    for key, value in display.items():
        if key in ("user_type", "guid", "origin", "server_env", "created_at",
                   "expires_at", "token", "signature"):
            continue
        rows.append(_splash_kv_row(key, value))
    return (
        '<h3 style="margin:16px 0 6px 0;color:#0b7a75;font-size:15px;">Identity</h3>'
        '<dl style="margin:0;display:grid;grid-template-columns:160px 1fr;gap:4px 12px;">'
        + ''.join(rows) +
        '</dl>'
    )


def _splash_kv_row(key: str, value) -> str:
    if isinstance(value, (dict, list)):
        v = _json.dumps(value, indent=2, ensure_ascii=False, default=str)
    else:
        v = str(value)
    return (
        f'<dt style="font-weight:600;color:#374151;">{_html.escape(str(key))}</dt>'
        f'<dd style="margin:0;font-family:ui-monospace,Menlo,Consolas,monospace;'
        f'font-size:12px;color:#1f2937;white-space:pre-wrap;word-break:break-all;">'
        f'{_html.escape(v)}'
        '</dd>'
    )


def _splash_history(sch: dict) -> str:
    ux_events = sch.get("ux_events") if isinstance(sch.get("ux_events"), list) else []
    utterances = sch.get("utterances") if isinstance(sch.get("utterances"), list) else []
    empty = (not ux_events) and (not utterances)
    intro = (
        '<h3 style="margin:14px 0 4px 0;color:#0b7a75;font-size:12px;">'
        'Session Conversation History'
        '</h3>'
    )
    if empty:
        return intro + (
            '<p style="margin:0;color:#9ca3af;font-size:10px;font-style:italic;">'
            'No UX events or utterances yet. Click around or type a prompt to see this grow.'
            '</p>'
        )

    person_items = _collect_person(utterances)
    person_to_system = _collect_person_to_system(ux_events)
    machine_items = _collect_machine(ux_events, utterances)
    llm_to_system = _collect_llm_to_system(utterances)

    threads_grid = (
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;">'
        + _render_thread("Person", "#0b7a75", "#f0fffe", person_items)
        + _render_thread("Person → System", "#0b7a75", "#e6fffd", person_to_system)
        + _render_thread("Machine", "#d97706", "#fff7ed", machine_items)
        + _render_thread("LLM → System", "#d97706", "#fef3c7", llm_to_system)
        + '</div>'
    )
    return intro + threads_grid


def _collect_person(utterances: list) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for u in utterances:
        if not isinstance(u, dict):
            continue
        at = str(u.get("at", ""))
        text = str(u.get("text", ""))
        out.append((at, f'"{_html.escape(text)}"'))
    out.sort(key=lambda r: r[0])
    return out


def _collect_person_to_system(ux_events: list) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for e in ux_events:
        if not isinstance(e, dict):
            continue
        at = str(e.get("at", ""))
        event_type = str(e.get("event_type", "?"))
        value = e.get("value")
        tail = ""
        if value is not None and value != "":
            v = _json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value)
            tail = f" · {v[:90]}"
        out.append((at, f"<strong>{_html.escape(event_type)}</strong>{_html.escape(tail)}"))
    out.sort(key=lambda r: r[0])
    return out


def _collect_machine(ux_events: list, utterances: list) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for e in ux_events:
        if not isinstance(e, dict):
            continue
        ped = e.get("pedantic_response")
        if not ped:
            continue
        at = str(e.get("at", ""))
        text = ped.get("text") if isinstance(ped, dict) else str(ped)
        out.append((at, _html.escape(str(text or "(empty pedantic response)"))))
    for u in utterances:
        if not isinstance(u, dict):
            continue
        br = u.get("bridge_response") or {}
        if isinstance(br, dict) and br.get("kind") == "tool_invocation":
            at = str(u.get("at", ""))
            tool_name = str(br.get("tool_name", "?"))
            tool_result = br.get("tool_result")
            tail = ""
            if tool_result is not None:
                tail = " → " + _json.dumps(tool_result, default=str)[:120]
            out.append((at, f"tool_result from <strong>{_html.escape(tool_name)}</strong>{_html.escape(tail)}"))
    out.sort(key=lambda r: r[0])
    return out


def _collect_llm_to_system(utterances: list) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for u in utterances:
        if not isinstance(u, dict):
            continue
        br = u.get("bridge_response") or {}
        if not isinstance(br, dict):
            continue
        kind = br.get("kind")
        at = str(u.get("at", ""))
        if kind == "llm_clarification":
            llm_text = str(br.get("llm_response", ""))
            info = br.get("info_sought")
            tail = ""
            if isinstance(info, list) and info:
                tail = " · seeking: " + ", ".join(map(str, info))
            out.append((at, f"<strong>→ Person:</strong> {_html.escape(llm_text + tail)}"))
        elif kind == "tool_invocation":
            tool_name = str(br.get("tool_name", "?"))
            tool_args = br.get("tool_args")
            args_tail = ""
            if tool_args is not None:
                args_tail = "(" + _json.dumps(tool_args, default=str)[:120] + ")"
            out.append((at, f"<strong>→ Machine:</strong> invoke <strong>{_html.escape(tool_name)}</strong>{_html.escape(args_tail)}"))
    out.sort(key=lambda r: r[0])
    return out


def _render_thread(title: str, color: str, bg: str, items: list[tuple[str, str]]) -> str:
    if not items:
        body = (
            '<div style="padding:4px 0;color:#9ca3af;font-size:9px;font-style:italic;">'
            '(no entries yet)</div>'
        )
    else:
        rows = []
        for i, (at, body_html) in enumerate(items):
            rows.append(
                f'<div style="padding:3px 0;border-top:{"1px solid #d1fae5" if i else "none"};">'
                f'<div style="font-size:9px;color:#6b7280;line-height:1.1;">{_html.escape(at)}</div>'
                f'<div style="font-size:10px;color:#1f2937;line-height:1.2;">{body_html}</div>'
                '</div>'
            )
        body = ''.join(rows)
    return (
        f'<div style="border-left:2px solid {color};background:{bg};padding:5px 6px;">'
        f'<div style="font-weight:600;color:{color};font-size:10px;margin-bottom:3px;text-transform:uppercase;letter-spacing:.4px;">'
        f'{title}'
        '</div>'
        f'<div style="max-height:140px;overflow-y:auto;padding-right:3px;">'
        f'{body}'
        '</div>'
        '</div>'
    )


# ────────────────────────────────────────────────────────────────────
# Op handlers — graph nodes
# ────────────────────────────────────────────────────────────────────

async def _handle_boot(deps: AgentDeps, payload: dict[str, Any]) -> Response:
    deps.stream({"kind": "boot", "data": {"ok": True}})
    return Response(kind="boot", result={"op": "boot"})


async def _handle_splash(deps: AgentDeps, payload: dict[str, Any]) -> Response:
    html = _splash_html(deps.user_object)
    log_ux_event(
        deps.user_object,
        "splash_displayed",
        value=None,
        pedantic_response={"text": _SPLASH_PEDANTIC},
    )
    deps.stream({"kind": "splash", "data": {"html": html}})
    return Response(kind="splash", result={"html": html})


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
