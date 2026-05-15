# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Session conversation history — tools that record human / machine / LLM
turns into admin.Sessions under the session's GUID.

Two action types (Skip's taxonomy, 2026-05-14):
  * UX-control interaction → recorded into
    `session_conversation_history.ux_events[]` with the tool's pedantic
    response paired inline on the entry.
  * Utterance (typed prompt) → recorded into
    `session_conversation_history.utterances[]` with the utterance bridge's
    response (tool_invocation | llm_clarification | unrouted).

Each tool subclasses `Tool` and declares pydantic request + response
models. The gate validates inputs + outputs; tools operate on validated
values only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar, Optional, Type

from pydantic import Field

from authentication.tool_framework import (
    HistoryAppend,
    Tool,
    ToolRequest,
    ToolResponse,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── shared helpers — used by other tools (e.g., SplashTool emits a
#    ux_event for its own invocation) ─────────────────────────────────

def ux_event_append(
    event_type: str,
    value: Any,
    pedantic_response: dict | str | None,
) -> HistoryAppend:
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


def utterance_append(
    text: str,
    bridge_response: dict | None = None,
) -> HistoryAppend:
    entry: dict[str, Any] = {
        "text": text,
        "at": _now_iso(),
    }
    if bridge_response is not None:
        entry["bridge_response"] = bridge_response
    return HistoryAppend(array="utterances", entry=entry)


# ── Tools ────────────────────────────────────────────────────────────

class RecordUtteranceRequest(ToolRequest):
    text: str = Field(min_length=1)


class RecordUtteranceResponse(ToolResponse):
    pass


class RecordUtteranceTool(Tool):
    """Capture a user utterance. Phase-1 bridge stub: bridge_response.kind
    = 'unrouted' until the utterance bridge is wired."""
    tool_name: ClassVar[str] = "record_utterance"
    request_model: ClassVar[Type[ToolRequest]] = RecordUtteranceRequest
    response_model: ClassVar[Type[ToolResponse]] = RecordUtteranceResponse

    def execute(self, session_token, user_object, request):
        text = request.text.strip()
        bridge_response = {
            "kind": "unrouted",
            "note": "Utterance bridge not yet wired; utterance captured for replay.",
        }
        return RecordUtteranceResponse(
            result={"op": self.tool_name, "ok": True},
            history_append=utterance_append(text, bridge_response=bridge_response),
        )


class RecordUxEventRequest(ToolRequest):
    event_type: str = Field(min_length=1)
    value: Optional[Any] = None
    pedantic_response: Optional[dict[str, Any]] = None


class RecordUxEventResponse(ToolResponse):
    pass


class RecordUxEventTool(Tool):
    """Capture a UX-control interaction (a tool the operator invoked via
    a UX control) into ux_events. The `event_type` is the tool name;
    `value` is the args; `pedantic_response` is what the tool returned."""
    tool_name: ClassVar[str] = "record_ux_event"
    request_model: ClassVar[Type[ToolRequest]] = RecordUxEventRequest
    response_model: ClassVar[Type[ToolResponse]] = RecordUxEventResponse

    def execute(self, session_token, user_object, request):
        return RecordUxEventResponse(
            result={"op": self.tool_name, "ok": True},
            history_append=ux_event_append(
                request.event_type,
                request.value,
                request.pedantic_response,
            ),
        )
