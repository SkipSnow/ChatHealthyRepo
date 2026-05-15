# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""AgentDeps — the runtime contract every tool receives after auth.

The AuthorizationsAndAuthentications tool runs first on every /gate hit,
establishes who the user is + what they can do (the UserObject), then
hands an AgentDeps to whichever downstream tool the UniversalNavigation
graph dispatches to.

`stream` is the NDJSON sink: tools call `deps.stream({...})` at any point
during their run() and the gate route forwards each event as a chunk on
the streamed response so the browser sees progress immediately.

`mongo_frontend` / `mongo_pipeline` are runtime resources. SharedServices
holds connections to both clusters (front-end carries admin.sessions,
pipeline carries SpecialtyMetaData + providers + everything else).
Tools that need data read it off these, not off positional args.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from authentication.user_object import UserObject
from chathealthy_frontend_lib.authentication.session_token import SessionToken


StreamSink = Callable[[dict[str, Any]], None]


@dataclass
class AgentDeps:
    """Passed to every tool's run() after AuthN completes."""
    user_object: UserObject
    session_token: SessionToken
    mongo_frontend: Any
    mongo_pipeline: Optional[Any]
    server_env: str
    stream: StreamSink


@dataclass
class AuthnDeps:
    """AuthN tool's own runtime resources. Constructed by the gate route
    from the incoming cookie + env + mongo client."""
    prior_guid: Optional[str]
    server_env: str
    mongo_frontend: Any


# ────────────────────────────────────────────────────────────────────
# Shared logging helpers — every tool (and the universal navigation
# orchestrator) writes to the user_object's session_conversation_history
# through these functions. Mutate-in-place; the gate persists at the end.
# ────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_utterance(user_object: UserObject, text: str) -> None:
    """Person stream: the typed text the user submitted via the prompt."""
    user_object.session_conversation_history.utterances.append({
        "text": text, "at": _now_iso(),
    })


def log_ux_event(
    user_object: UserObject,
    event_type: str,
    value: Any = None,
    pedantic_response: Any = None,
) -> None:
    """Person → System stream (+ Machine when a pedantic_response is given).
    The user clicked a UX control; the system may have a pedantic textual
    response paired inline."""
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
    user_object.session_conversation_history.ux_events.append(entry)


def log_tool_invocation(
    user_object: UserObject,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_result: Any,
) -> None:
    """LLM → System (invocation side) + Machine (tool_result side).
    Every tool the navigation orchestrator dispatches to should call this
    after running so the user_object carries the full audit trail."""
    user_object.session_conversation_history.utterances.append({
        "text": f"{tool_name}",
        "at": _now_iso(),
        "bridge_response": {
            "kind": "tool_invocation",
            "tool_name": tool_name,
            "tool_args": tool_args,
            "tool_result": tool_result,
        },
    })


def log_llm_clarification(
    user_object: UserObject,
    llm_response: str,
    info_sought: Optional[list[str]] = None,
) -> None:
    """LLM → System: the LLM emitted a clarification back to the person."""
    user_object.session_conversation_history.utterances.append({
        "text": llm_response,
        "at": _now_iso(),
        "bridge_response": {
            "kind": "llm_clarification",
            "llm_response": llm_response,
            "info_sought": info_sought or [],
        },
    })
