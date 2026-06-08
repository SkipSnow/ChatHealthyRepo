# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""SpecialtyFilter tool — turns the user's latest utterance into NUCC
specialty codes + location, then writes the result onto user_object.

The Agent contract is user_object-based: deps.user_object is the input
substrate; this tool reads `utterances[-1].text` as the user's query
and writes the picked codes into `user_object.find_care`. Iteration 1
internally HTTP-calls the existing FindCare `/classify` endpoint so the
LLM-picks pipeline (Gemini Flash Lite preview + find_specialists.py
engine) is reused verbatim and behavior is preserved exactly. Iteration
2 pulls that engine in-process and removes the HTTP hop.

Canonical *_tool.py exports: TOOL_NAME, Request, Response, run().
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from authentication.agent_deps import AgentDeps
from authentication.chathealthy_tool import ChatHealthyTool

_log = logging.getLogger("shared_services.specialty_filter")

_FINDCARE_INTERNAL_URL_ENV = "FINDCARE_INTERNAL_URL"
_FINDCARE_INTERNAL_URL_DEFAULT = "https://ch-findcare:7860"


class Request(BaseModel):
    """No payload — the tool reads its input off user_object.utterances[-1]."""
    model_config = {"extra": "ignore"}


class SpecialtyRow(BaseModel):
    code: str
    name: str
    can_prescribe: Optional[bool] = None
    homeopathic: Optional[bool] = None
    rank: Optional[int] = None
    homeopathic_general: Optional[bool] = None


class Response(BaseModel):
    specialties: list[SpecialtyRow] = Field(default_factory=list)
    homeopathic_generalists: list[SpecialtyRow] = Field(default_factory=list)
    model: Optional[str] = None
    error: Optional[str] = None


def _findcare_url() -> str:
    return os.environ.get(_FINDCARE_INTERNAL_URL_ENV) or _FINDCARE_INTERNAL_URL_DEFAULT


def _latest_utterance_text(deps: AgentDeps) -> str:
    utterances = deps.user_object.session_conversation_history.utterances
    if not utterances:
        return ""
    last = utterances[-1]
    if isinstance(last, dict):
        return str(last.get("text", "")).strip()
    return ""


class SpecialtyFilterTool(ChatHealthyTool):
    """Vernacular text → NUCC specialty codes. Reads the user's latest
    utterance off deps.user_object; HTTP-calls the existing /classify
    engine; emits a stream event so the FE renders the filter as soon as
    picks arrive."""
    TOOL_NAME = "specialty_filter"
    # Reference the module-level pydantic models so they remain importable
    # via the canonical `<module>.Request` / `.Response` path.
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: Optional["Request"] = None) -> "Response":
        text = _latest_utterance_text(deps)
        if not text:
            resp = self.Response(error="No utterance text on user_object.")
            deps.stream({"kind": "specialties", "data": resp.model_dump(exclude_none=True)})
            return resp

        url = _findcare_url() + "/classify"
        try:
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                r = await client.post(url, json={"message": text})
                r.raise_for_status()
                raw = r.json()
        except Exception as exc:
            _log.error("specialty_filter HTTP /classify failed: %s: %s",
                       type(exc).__name__, exc)
            resp = self.Response(error=f"classify_unavailable: {type(exc).__name__}")
            deps.stream({"kind": "specialties", "data": resp.model_dump(exclude_none=True)})
            return resp

        specialties = [SpecialtyRow(**s) for s in (raw.get("specialties") or [])]
        homeo = [SpecialtyRow(**s) for s in (raw.get("homeopathic_generalists") or [])]
        resp = self.Response(
            specialties=specialties,
            homeopathic_generalists=homeo,
            model=raw.get("model"),
            error=raw.get("error"),
        )
        deps.stream({"kind": "specialties", "data": resp.model_dump(exclude_none=True)})
        return resp


TOOL = SpecialtyFilterTool()
