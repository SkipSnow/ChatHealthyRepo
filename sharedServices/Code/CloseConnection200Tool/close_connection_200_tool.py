# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""CloseConnection200Tool — closes the StreamingResponse with HTTP 200 OK.

Dispatched by UR when the IntentDocument's target_action is
"closeConnection200". The tool has no knowledge of what was said to the
user or what the user said to elicit the response; its only job is to
write a final framing event onto the stream so the StreamingResponse
terminates cleanly with HTTP 200 OK.
"""
from __future__ import annotations

import asyncio
from chathealthy_lib import ChatHealthyLoggingService

from pydantic import BaseModel

from chathealthy_lib.authentication.agent_deps import AgentDeps
from chathealthy_lib.authentication.chathealthy_tool import ChatHealthyTool

log = ChatHealthyLoggingService()


class Request(BaseModel):
    """No payload."""
    model_config = {"extra": "ignore"}


class Response(BaseModel):
    closed: bool


class CloseConnection200Tool(ChatHealthyTool):
    TOOL_NAME = "close_connection_200"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        # Final framing event onto the stream. The /gate pipeline pushes its
        # own kind:"final" event with session metadata after the run chain
        # completes; this kind:"close" event is the explicit close signal
        # the FE can observe.
        deps.stream({"kind": "close", "data": {"status": 200}})

        # Streaming-contract flush.
        await asyncio.sleep(0)

        return self.Response(closed=True)


TOOL = CloseConnection200Tool()
