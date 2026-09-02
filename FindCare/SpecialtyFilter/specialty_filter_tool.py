# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""SpecialtyFilter tool — turns the user's latest utterance into NUCC
specialty codes + location, then writes the result onto user_object.

The Agent contract is user_object-based: deps.user_object is the input
substrate; this tool reads `utterances[-1].text` as the user's query
and writes the picked codes into `user_object.find_care`. Iteration 1
internally HTTP-calls the existing FindCare `/classify` endpoint so the
specialty pipeline (normalize + embed + $vectorSearch + LLM filter, filter.py
engine) is reused verbatim and behavior is preserved exactly. Iteration
2 pulls that engine in-process and removes the HTTP hop.

Canonical *_tool.py exports: TOOL_NAME, Request, Response, run().
"""
from __future__ import annotations

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
import os
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from chathealthy_lib.authentication.agent_deps import AgentDeps
from chathealthy_lib.authentication.chathealthy_tool import ChatHealthyTool

log = ChatHealthyLoggingService()


FINDCARE_INTERNAL_URL_ENV = "FINDCARE_INTERNAL_URL"
FINDCARE_INTERNAL_URL_DEFAULT = "https://ch-findcare:7860"



def _ch_exc():
    """ChatHealthyException without assuming the library is installed.
    These modules run as bare scripts in the devops chain."""
    import sys as _s, pathlib as _p
    for _d in _p.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _l = _d / "ChatHealthyLib" / "src"
            if str(_l) not in _s.path:
                _s.path.insert(0, str(_l))
            break
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException


class Request(BaseModel):
    """The natural-language complaint UM extracted from the user's utterance.
    Required and non-empty; SpecialtyFilter does not source from any other
    field on user_object."""
    model_config = {"extra": "ignore"}
    section: str = Field(
        default="Individual",
        description="Which partition of the catalogue this resolution "
                    "reads: Individual for a care giver, Non-Individual "
                    "for a facility. The same funnel runs either way; this "
                    "is the one thing its queries differ by.")
    query: str = Field(
        description="The kind of care wanted, in clinical terms rather than "
                    "the words the person used -- \"psychological problem\", "
                    "not \"shrink\". Returns the provider types that treat "
                    "it. Carries no location and finds no providers.")


class SpecialtyRow(BaseModel):
    code: str
    name: str
    can_prescribe: Optional[bool] = None
    homeopathic: Optional[bool] = None
    rank: Optional[int] = None
    homeopathic_general: Optional[bool] = None


class Response(BaseModel):
    # What the user's words MEAN, in clinical terms: 'shrink' arrives here
    # as 'psychological problem'. The utterance stays in the conversation;
    # this is the translated fact every other tool reads.
    complaint: str = ""
    specialties: list[SpecialtyRow] = Field(default_factory=list)
    homeopathic_generalists: list[SpecialtyRow] = Field(default_factory=list)
    model: Optional[str] = None
    error: Optional[str] = None


def findcare_url() -> str:
    return os.environ.get(FINDCARE_INTERNAL_URL_ENV) or FINDCARE_INTERNAL_URL_DEFAULT


class SpecialtyFilterTool(ChatHealthyTool):
    @staticmethod
    def _broadcast(deps, section: str, event: dict) -> None:
        """The offered-kinds panel belongs to the care-giver page.

        A facility resolution runs the same funnel but paints no such
        panel: emitting one would repaint the specialty panel the person
        is looking at with facility kinds they never asked to choose
        among.
        """
        if section == "Individual":
            deps.stream(event)

    """Vernacular text → NUCC specialty codes. Consumes the UM-extracted
    complaint phrase via Request.query; HTTP-calls the existing /classify
    engine; emits a stream event so the FE renders the filter as soon as
    picks arrive."""
    TOOL_NAME = "specialty_filter"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        text = (request.query or "").strip()

        if not text:
            raise ChatHealthyException(
            mode="value_error",
            component="specialty_filter_tool",
            message="SpecialtyFilter requires a non-empty Request.query; UR "
                "must pass the UM-extracted complaint phrase.")

        url = findcare_url() + "/classify"
        try:
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                # The token this hop already holds, forwarded so FindCare
                # can verify the SharedServices signature on it. This is
                # the fourth cross-service hop, and it needs the token for
                # the same reason the provider search, the provider detail
                # and the clinical-trials dispatcher do.
                r = await client.post(url, json={
                    "message": text,
                    "section": request.section,
                    "session_token": deps.session_token.model_dump(mode="json"),
                })
                r.raise_for_status()
                raw = r.json()
        except Exception as exc:
            # Mode 2 (REQ-B-008): LLM /classify temporarily unavailable; the
            # tool returns a graceful Response.error to the user inline. NOT
            # a 503; do NOT tag fatal_error=True.
            resp = self.Response(error=f"classify_unavailable: {type(exc).__name__}")
            self._broadcast(deps, request.section, {"kind": "specialties", "data": resp.model_dump(exclude_none=True)})
            return resp

        specialties = [SpecialtyRow(**s) for s in (raw.get("specialties") or [])]
        homeo = [SpecialtyRow(**s) for s in (raw.get("homeopathic_generalists") or [])]
        resp = self.Response(
            complaint=str(raw.get("complaint") or "").strip(),
        specialties=specialties,
            homeopathic_generalists=homeo,
            model=raw.get("model"),
            error=raw.get("error"),
        )
        self._broadcast(deps, request.section, {"kind": "specialties", "data": resp.model_dump(exclude_none=True)})
        return resp


TOOL = SpecialtyFilterTool()
