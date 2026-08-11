# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""ClinicalTrials dispatcher.

SharedServices does NOT host the clinical-trials tool. This dispatcher
forwards a findClinicalTrials utterance to the FindCare backend's
/clinical_trials endpoint over HTTP, then streams the FindCare
response's NDJSON chunk events back into the user's /gate stream so
the React widget receives them transparently.

Canonical *_tool.py exports: TOOL_NAME, Request, Response, run().
"""
from __future__ import annotations

import json as _json
import os
from typing import Any, Optional

import httpx
from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
from pydantic import BaseModel, Field

from chathealthy_lib.authentication.agent_deps import AgentDeps
from chathealthy_lib.authentication.chathealthy_tool import ChatHealthyTool

log = ChatHealthyLoggingService()


FINDCARE_INTERNAL_URL_ENV = "FINDCARE_INTERNAL_URL"
FINDCARE_INTERNAL_URL_DEFAULT = "https://ch-findcare:7860"


def findcare_url() -> str:
    return os.environ.get(FINDCARE_INTERNAL_URL_ENV) or FINDCARE_INTERNAL_URL_DEFAULT


class Request(BaseModel):
    condition: str
    user_location: Optional[str] = None
    age_years: Optional[int] = None
    sex: Optional[str] = None
    geographic_scope: Optional[str] = None
    page_size: int = 10
    cursor: Optional[str] = None


class Response(BaseModel):
    error: Optional[str] = None


class ClinicalTrialsDispatcher(ChatHealthyTool):
    TOOL_NAME = "clinical_trials_dispatcher"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        url = findcare_url() + "/clinical_trials"
        body = request.model_dump(exclude_none=True)
        try:
            async with httpx.AsyncClient(timeout=None, verify=False) as client:
                async with client.stream("POST", url, json=body) as resp:
                    resp.raise_for_status()
                    async for raw_line in resp.aiter_lines():
                        if not raw_line.strip():
                            continue
                        try:
                            evt = _json.loads(raw_line)
                        except _json.JSONDecodeError:
                            continue
                        deps.stream(evt)
            return Response()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.WriteTimeout, httpx.PoolTimeout, httpx.ReadError,
                httpx.WriteError, httpx.RemoteProtocolError,
                httpx.HTTPStatusError) as exc:
            log.error(
                "FindCare /clinical_trials call failed: %s: %s",
                type(exc).__name__, exc,
                exc=ChatHealthyException(
                    mode="clinical_trials_unavailable",
                    message=f"FindCare /clinical_trials call failed: {type(exc).__name__}: {exc}",
                    component="ClinicalTrialsDispatcher",
                    exception=exc,
                ), if_not_debug_log=True,
            )
            err = "Clinical trials lookup is unavailable right now. Please try again in a moment."
            # Emit a single final empty chunk so the widget stops the
            # spinner and shows the error.
            deps.stream({
                "kind": "clinical_trials_chunk",
                "data": {
                    "trials": [],
                    "chunk_index": 0,
                    "is_final": True,
                    "total_eligible": 0,
                    "is_partial": False,
                    "error": err,
                },
            })
            return Response(error=err)


TOOL = ClinicalTrialsDispatcher()
