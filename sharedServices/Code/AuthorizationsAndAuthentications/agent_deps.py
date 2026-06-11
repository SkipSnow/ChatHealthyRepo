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

`mongo_frontend` is the front-end cluster handle. SharedServices runtime
talks only to the front-end cluster (Users.sessions, SpecialtyMetaData,
providers, and everything else the front-end app reads); the pipeline
cluster is ETL-only and is never reachable from a runtime tool.
Tools that need data read it off `mongo_frontend`, not off positional args.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from authentication.user_object import (
    Action,
    UserObject,
    Utterance,
)
from chathealthy_frontend_lib.authentication.session_token import SessionToken


StreamSink = Callable[[dict[str, Any]], None]


@dataclass
class AgentDeps:
    """Passed to every tool's run() after AuthN completes."""
    user_object: UserObject
    session_token: SessionToken
    mongo_frontend: Any
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
# Two-bucket session-history append helpers. utterances + actions live
# on user_object.session_conversation_history; each bucket carries its
# own per-session monotonic sequence number computed at append time.
# Timestamps render in Pacific time with a literal " PST" suffix per
# Skip 2026-06-10 — the offset is unambiguous regardless of DST and
# the suffix keeps the string human-readable.
# ────────────────────────────────────────────────────────────────────


_PT = ZoneInfo("America/Los_Angeles")


def _now_pst() -> str:
    """ISO-8601 local Pacific time + literal ' PST' suffix."""
    return datetime.now(_PT).isoformat() + " PST"


import logging as _logging
_log = _logging.getLogger("shared_services.session_history")


def _next_session_event_n(user_object: UserObject) -> int:
    """ONE global session-wide event counter shared across both buckets.
    Computed as max(n) over the union of utterances and actions, plus
    one. Per-bucket counters (the prior design) collided on n=1, n=2
    between buckets and made the chronological narrative impossible to
    reconstruct; a single sequence interleaves the two streams so
    sort-by-n recovers the actual order of events.
    """
    sch = user_object.session_conversation_history
    max_n = 0
    for u in sch.utterances:
        if u.n > max_n:
            max_n = u.n
    for a in sch.actions:
        if a.n > max_n:
            max_n = a.n
    return max_n + 1


def append_person_utterance(user_object: UserObject, text: str) -> None:
    bucket = user_object.session_conversation_history.utterances
    n = _next_session_event_n(user_object)
    bucket.append(Utterance(
        n=n,
        at=_now_pst(),
        actor="person",
        text=text,
    ))
    _log.debug(
        "append_person_utterance n=%d text=%r total_utterances=%d",
        n, text[:80], len(bucket),
    )


def append_system_utterance(user_object: UserObject, text: str) -> None:
    bucket = user_object.session_conversation_history.utterances
    n = _next_session_event_n(user_object)
    bucket.append(Utterance(
        n=n,
        at=_now_pst(),
        actor="system",
        text=text,
    ))
    _log.debug(
        "append_system_utterance n=%d text=%r total_utterances=%d",
        n, text[:80], len(bucket),
    )


def append_action(
    user_object: UserObject,
    tool_name: str,
    input_json: Optional[dict[str, Any]] = None,
    output_json: Optional[dict[str, Any]] = None,
    at: Optional[str] = None,
) -> None:
    """Append a system action. tool_name='on_load' for action #1 of a new
    session; otherwise the dispatched tool's TOOL_NAME or 'ux_event' for
    a recorded user gesture. n is drawn from the single session-wide
    sequence (shared with utterances) so the narrative is reconstructible
    by sorting the union of both buckets by n."""
    bucket = user_object.session_conversation_history.actions
    n = _next_session_event_n(user_object)
    bucket.append(Action(
        n=n,
        at=at if at is not None else _now_pst(),
        tool_name=tool_name,
        input_json=input_json or {},
        output_json=output_json or {},
    ))
