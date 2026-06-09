# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""NonsenseTool — increments the silly-question counter on user_object.

Dispatched by UR when the IntentDocument's target_action is "nonsense".
Deploy-1 behavior is just the counter bump; later behavior may add user-
facing messaging or analytics.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pydantic import BaseModel

from authentication.agent_deps import AgentDeps
from authentication.chathealthy_tool import ChatHealthyTool
from authentication.user_object import SillyQuestionCounts

from UtteranceManager.intent_document import (
    Argument,
    IntentCloseConnection200,
    IntentDocument,
)

_log = logging.getLogger("shared_services.nonsense_tool")


_NONSENSE_CLARIFICATION_TEXT = (
    "I didn't catch that. To help you find a provider I need two things: "
    "(a) a complaint — the symptom or condition you're concerned about — "
    "and (b) a location: a state, city + state, county + state, or 5-digit "
    "ZIP code."
)


class Request(BaseModel):
    """No payload — NonsenseTool reads its data off deps.user_object.intent."""
    model_config = {"extra": "ignore"}


class Response(BaseModel):
    session_total: int
    current_sequence_total: int


class NonsenseTool(ChatHealthyTool):
    """Increments silly_question_counts on user_object, streams a hardcoded
    clarification prompt to the user, and morphs the IntentDocument so
    target_action becomes closeConnection200 (UR chains to
    CloseConnection200Tool on the next dispatch hop)."""

    TOOL_NAME = "nonsense_tool"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        counts = deps.user_object.silly_question_counts or SillyQuestionCounts()
        counts.session_total = counts.session_total + 1
        counts.current_sequence_total = counts.current_sequence_total + 1
        deps.user_object.silly_question_counts = counts

        deps.stream({
            "kind": "prompt",
            "data": {"text": _NONSENSE_CLARIFICATION_TEXT},
        })
        await asyncio.sleep(0)

        document = deps.user_object.intent
        close_entry = IntentCloseConnection200(
            name="closeConnection200",
            arguments=[
                Argument(
                    name="close_connection",
                    value="true",
                    type="boolean",
                    required=True,
                ),
            ],
        )
        if document is None:
            new_doc = IntentDocument(
                target_action="closeConnection200",
                intents=[close_entry],
            )
        else:
            keep = [i for i in document.intents if i.name != "closeConnection200"]
            keep.append(close_entry)
            new_doc = IntentDocument(
                target_action="closeConnection200",
                intents=keep[-3:],
            )
        deps.user_object.intent = new_doc

        return self.Response(
            session_total=counts.session_total,
            current_sequence_total=counts.current_sequence_total,
        )


TOOL = NonsenseTool()
