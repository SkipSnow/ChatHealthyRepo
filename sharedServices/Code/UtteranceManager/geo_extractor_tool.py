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
from chathealthy_frontend_lib import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException
from typing import Optional

from pydantic import BaseModel

from authentication.agent_deps import AgentDeps
from authentication.chathealthy_tool import ChatHealthyTool
from chathealthy_frontend_lib.geo_extractor import extract_location

log = ChatHealthyLoggingService()


class Request(BaseModel):
    model_config = {"extra": "ignore"}


class Response(BaseModel):
    state: Optional[str] = None
    county: Optional[str] = None
    city: Optional[str] = None
    zip: Optional[str] = None
    error: Optional[str] = None


def latest_utterance_text(deps: AgentDeps) -> str:
    """Return the most recent PERSON utterance text on the session.
    System turns and earlier person turns are skipped."""
    utterances = deps.user_object.session_conversation_history.utterances
    for entry in reversed(utterances):
        actor = getattr(entry, "actor", None) or (entry.get("actor") if isinstance(entry, dict) else None)
        if actor != "person":
            continue
        text = getattr(entry, "text", None) or (entry.get("text") if isinstance(entry, dict) else "")
        return str(text or "").strip()
    return ""


class GeoExtractorTool(ChatHealthyTool):
    TOOL_NAME = "geo_extractor"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: Optional["Request"] = None) -> "Response":
        text = latest_utterance_text(deps)
        if not text:
            return self.Response(error="No utterance text on user_object.")
        try:
            out = await asyncio.to_thread(extract_location, text)
        except Exception as exc:
            log.error("geo_extractor failed: %s: %s", type(exc).__name__, exc, exc=ChatHealthyException(
                                                                                mode="geo_extractor_failed",
                                                                                message=f"geo_extractor failed: {type(exc).__name__}: {exc}",
                                                                                component="GeoExtractorTool",
                                                                                exception=exc,
                                                                            ), if_not_debug_log=True)
            return self.Response(error=f"geo_unavailable: {type(exc).__name__}")
        return self.Response(
            state=out.state,
            county=out.county,
            city=out.city,
            zip=out.zip,
        )


TOOL = GeoExtractorTool()
