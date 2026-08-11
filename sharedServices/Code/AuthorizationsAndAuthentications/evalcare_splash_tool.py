# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""EvalCare splash tool wrapper — SharedServices side.

Receives a click-path payload from the UniversalNavigation tool router
(op == 'evalcare-splash'), HTTPS-hops into EvaluateCare's /splash
endpoint, and returns the splash JSON to the orchestrator. No LLM.
No utterance manager involvement — this is the click path.

EPIC-002-F-004-S-001 universal-gateway rule: client-to-server calls
route through SharedServices /gate; SharedServices performs the
server-to-server hop on behalf of the client.
"""
from __future__ import annotations

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
import os
from typing import Any, Optional

import httpx
from pydantic import BaseModel

from chathealthy_lib.authentication.agent_deps import AgentDeps
from chathealthy_lib.authentication.chathealthy_tool import ChatHealthyTool

log = ChatHealthyLoggingService()


EVALCARE_INTERNAL_URL_ENV = "EVALCARE_INTERNAL_URL"
EVALCARE_INTERNAL_URL_DEFAULT = "https://ch-evalcare:7860"


class Request(BaseModel):
    """No fields — banner click carries no body."""
    pass


class Response(BaseModel):
    # Tool returns STRUCTURED DATA ONLY. React widget renders.
    data: dict = {}
    error: Optional[str] = None


def evalcare_url() -> str:
    return os.environ.get(EVALCARE_INTERNAL_URL_ENV) or EVALCARE_INTERNAL_URL_DEFAULT


class EvalCareSplashTool(ChatHealthyTool):
    TOOL_NAME = "evalcare_splash"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        url = evalcare_url() + "/splash"
        try:
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                r = await client.post(url, json={})
                r.raise_for_status()
                raw = r.json()
        except Exception as exc:
            # Mode 2 (REQ-B-008): EC /splash temporarily unavailable; tool
            # returns graceful Response.error inline. NOT 503; no fatal_error.
            log.error("evalcare_splash HTTP /splash failed: %s: %s",
                       type(exc).__name__, exc,
                       exc=ChatHealthyException(
                        mode="evalcare_splash_unavailable",
                        message=f"evalcare_splash HTTP /splash failed: {type(exc).__name__}: {exc}",
                        component="EvalCareSplashTool",
                        exception=exc,
                    ), if_not_debug_log=True)
            resp = self.Response(error=f"splash_unavailable: {type(exc).__name__}")
            deps.stream({"kind": "evalcare-splash", "data": resp.model_dump(exclude_none=True)})
            return resp

        # Pass through whatever structured payload EvaluateCare returned.
        # No HTML — the React widget is the renderer.
        resp = self.Response(data=raw if isinstance(raw, dict) else {})
        deps.stream({"kind": "evalcare-splash", "data": resp.model_dump(exclude_none=True)})
        return resp


TOOL = EvalCareSplashTool()
