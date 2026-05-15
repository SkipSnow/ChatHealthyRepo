# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Tool framework — the structured foundation under the universal gate.

Every operation the gate dispatches to is a `Tool` subclass with:

  * `tool_name`     — the string the wire uses to invoke it (e.g., 'boot',
                      'splash', 'record_utterance').
  * `request_model` — a pydantic model that validates the per-tool payload
                      delivered in `body.payload`.
  * `response_model`— a pydantic model that validates whatever the tool
                      returns. Wraps an optional `result` dict (for op-
                      specific payload bound for the client) and an
                      optional `history_append` directive (the gate
                      performs the `$push` to admin.Sessions).
  * `execute(...)`  — the body. Pure function of the session token, the
                      already-resolved user object doc, and the
                      validated request. Returns the response model.

The gate validates inbound and outbound: malformed payloads 400, malformed
tool responses raise loudly (we treat that as a bug, not a runtime
condition). This is the contract OAuth will plug into without ceremony.

Skip-agreed framework, 2026-05-14.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal, Optional, Type

from pydantic import BaseModel, Field


class ToolRequest(BaseModel):
    """Base class for every tool's per-request payload model."""
    model_config = {"extra": "forbid"}


class HistoryAppend(BaseModel):
    """One entry to $push onto session_conversation_history.<array>."""
    array: Literal["ux_events", "utterances"]
    entry: dict[str, Any]


class ToolResponse(BaseModel):
    """Base class for every tool's response model.

    `result` carries op-specific data destined for the client (e.g., the
    rendered HTML for splash). `history_append`, if present, is the
    single conversation-history entry the gate writes to admin.Sessions
    after execute() returns; the entry is appended to the named array
    by the gate, not by the tool.
    """
    result: dict[str, Any] = Field(default_factory=dict)
    history_append: Optional[HistoryAppend] = None


class GateRequest(BaseModel):
    """Outer-envelope request model — the gate's single nullable entrance.

    The FastAPI route layer fills `prior_guid` from the ch_session cookie
    and `server_env` from the runtime env var; the client supplies `op`
    and `payload`. The whole envelope MAY be null (a first-time arrival
    with no cookie and no body) — GateTool.execute(None) handles that.
    """
    model_config = {"extra": "ignore"}

    op: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    prior_guid: Optional[str] = None
    server_env: str = "dev"


class GateResponse(BaseModel):
    """Outer-envelope response shape the gate returns to the route layer."""
    guid: str
    user_object: dict[str, Any]
    result: dict[str, Any] = Field(default_factory=dict)


class Tool(ABC):
    """Abstract base for every gate-dispatched tool.

    Subclasses declare three class-level attributes plus an execute()
    body. The gate validates the inbound payload against `request_model`
    and the outbound return against `response_model`; subclass authors
    operate on validated values only.
    """
    tool_name: ClassVar[str]
    request_model: ClassVar[Type[ToolRequest]]
    response_model: ClassVar[Type[ToolResponse]]

    @abstractmethod
    def execute(
        self,
        session_token: Any,           # SessionToken; Any to avoid circular import
        user_object: dict[str, Any],
        request: ToolRequest,
    ) -> ToolResponse:
        ...

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for attr in ("tool_name", "request_model", "response_model"):
            if not getattr(cls, attr, None):
                raise TypeError(
                    f"Tool subclass {cls.__name__} missing required "
                    f"class attribute {attr!r}"
                )
