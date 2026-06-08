# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""GeoExtractor tool wrapper for the SharedServices UtteranceManager.

Thin ChatHealthyTool subclass that:
  * reads the latest utterance text off deps.user_object,
  * calls the in-process geo_extractor lib function on a worker thread,
  * returns a Response carrying state / county / city / zip.

The actual extraction lives in
``chathealthy_frontend_lib.geo_extractor.extract_location`` so any
ChatHealthy component can call the same function without crossing a
service boundary.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pydantic import BaseModel

from authentication.agent_deps import AgentDeps
from authentication.chathealthy_tool import ChatHealthyTool
from chathealthy_frontend_lib.geo_extractor import extract_location

_log = logging.getLogger("shared_services.geo_extractor")


class Request(BaseModel):
    model_config = {"extra": "ignore"}


class Response(BaseModel):
    state: Optional[str] = None
    county: Optional[str] = None
    city: Optional[str] = None
    zip: Optional[str] = None
    error: Optional[str] = None


def _latest_utterance_text(deps: AgentDeps) -> str:
    utterances = deps.user_object.session_conversation_history.utterances
    if not utterances:
        return ""
    last = utterances[-1]
    if isinstance(last, dict):
        return str(last.get("text", "")).strip()
    return ""


class GeoExtractorTool(ChatHealthyTool):
    TOOL_NAME = "geo_extractor"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: Optional["Request"] = None) -> "Response":
        text = _latest_utterance_text(deps)
        if not text:
            return self.Response(error="No utterance text on user_object.")
        try:
            out = await asyncio.to_thread(extract_location, text)
        except Exception as exc:
            _log.error("geo_extractor failed: %s: %s", type(exc).__name__, exc)
            return self.Response(error=f"geo_unavailable: {type(exc).__name__}")
        return self.Response(
            state=out.state,
            county=out.county,
            city=out.city,
            zip=out.zip,
        )


TOOL = GeoExtractorTool()
