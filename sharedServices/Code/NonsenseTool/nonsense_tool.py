# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""NonsenseTool — increments the silly-question counter on user_object.

Dispatched by UR when the IntentDocument's target_action is "nonsense".
Streams nothing user-visible — the LLM-authored top-level user_message
on the IntentDocument is the only chat prose for this turn (streamed by
UM before NonsenseTool runs). NonsenseTool morphs the IntentDocument so
target_action becomes closeConnection200; UR chains to
CloseConnection200Tool on the next dispatch hop.
"""
from __future__ import annotations

import asyncio
from chathealthy_frontend_lib import ChatHealthyLoggingService

from pydantic import BaseModel

from authentication.agent_deps import AgentDeps
from authentication.chathealthy_tool import ChatHealthyTool
from authentication.user_object import SillyQuestionCounts

from UtteranceManager.intent_document import (
    Argument,
    IntentCloseConnection200,
    IntentDocument,
)

log = ChatHealthyLoggingService()

class Request(BaseModel):
    """No payload — NonsenseTool reads its data off deps.user_object.intent."""
    model_config = {"extra": "ignore"}


class Response(BaseModel):
    session_total: int
    current_sequence_total: int


class NonsenseTool(ChatHealthyTool):
    """Increments silly_question_counts on user_object and morphs the
    IntentDocument so target_action becomes closeConnection200. Streams
    no chat prose — UM already streamed user_message before dispatch."""

    TOOL_NAME = "nonsense_tool"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        counts = deps.user_object.silly_question_counts or SillyQuestionCounts()
        counts.session_total = counts.session_total + 1
        counts.current_sequence_total = counts.current_sequence_total + 1
        deps.user_object.silly_question_counts = counts

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
                user_message=None,
            )
        else:
            keep = [i for i in document.intents if i.name != "closeConnection200"]
            keep.append(close_entry)
            # Carry forward the prior user_message — UM already streamed it.
            # Clear it on the morph so UR does not re-stream it on the
            # closeConnection200 hop.
            new_doc = IntentDocument(
                target_action="closeConnection200",
                intents=keep[-3:],
                user_message=None,
            )
        deps.user_object.intent = new_doc

        await asyncio.sleep(0)
        return self.Response(
            session_total=counts.session_total,
            current_sequence_total=counts.current_sequence_total,
        )


TOOL = NonsenseTool()
