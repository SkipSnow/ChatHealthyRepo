# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""ProviderSearchAndSelection tool — queries the provider DB given the
specialty codes + location resolved by SpecialtyFilter, returns the
provider list payload the FE's center panel renders.

The Agent contract is user_object-based: the prior tool (SpecialtyFilter)
wrote the codes + state to its own response object, which Universal-
Navigation hands here as the Request body. Internally, iteration 1 calls
the existing FindCare `/search` endpoint over HTTP for byte-identical
behavior. Iteration 2 inlines the search.

Canonical *_tool.py exports: TOOL_NAME, Request, Response, run().
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

from authentication.agent_deps import AgentDeps
from authentication.chathealthy_tool import ChatHealthyTool

_log = logging.getLogger("shared_services.provider_search")

_FINDCARE_INTERNAL_URL_ENV = "FINDCARE_INTERNAL_URL"
_FINDCARE_INTERNAL_URL_DEFAULT = "https://ch-findcare:7860"


class Request(BaseModel):
    specialty_codes: list[str] = Field(default_factory=list)
    state: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None
    zip: Optional[str] = None
    limit: int = 25
    after_npi: Optional[str] = None


class Response(BaseModel):
    providers: list[dict] = Field(default_factory=list)
    has_more: bool = False
    first_npi: Optional[str] = None
    last_npi: Optional[str] = None
    count: int = 0
    total_count: int = 0
    page_start: int = 1
    page_end: int = 0
    search_params: Optional[dict] = None
    specialization_options: Optional[list[dict]] = None
    state: Optional[str] = None
    error: Optional[str] = None


def _findcare_url() -> str:
    return os.environ.get(_FINDCARE_INTERNAL_URL_ENV) or _FINDCARE_INTERNAL_URL_DEFAULT


class ProviderSearchAndSelectionTool(ChatHealthyTool):
    """Pure-DB tool (no LLM Agent inside). Given specialty codes + state
    from deps.user_object.find_care (or via Request), HTTP-calls FindCare
    /search and returns the providers list. Same ChatHealthyTool interface
    as LLM-driven tools."""
    TOOL_NAME = "provider_search_and_selection"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        if not request.specialty_codes:
            resp = self.Response(error="No specialty codes; cannot search providers.")
            deps.stream({"kind": "providers", "data": resp.model_dump(exclude_none=True)})
            return resp

        body: dict[str, Any] = {
            "nucc_codes": request.specialty_codes,
            "limit": request.limit,
        }
        if request.state:
            body["state"] = request.state
        if request.city:
            body["city"] = request.city
        if request.county:
            body["county"] = request.county
        if request.zip:
            body["zip"] = request.zip
        if request.after_npi:
            body["after_npi"] = request.after_npi

        url = _findcare_url() + "/search"
        try:
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                r = await client.post(url, json=body)
                r.raise_for_status()
                raw = r.json()
        except Exception as exc:
            _log.error("provider_search HTTP /search failed: %s: %s",
                       type(exc).__name__, exc)
            resp = self.Response(error=f"search_unavailable: {type(exc).__name__}")
            deps.stream({"kind": "providers", "data": resp.model_dump(exclude_none=True)})
            return resp

        resp = self.Response(
            providers=raw.get("providers") or [],
            has_more=bool(raw.get("has_more", False)),
            first_npi=raw.get("first_npi"),
            last_npi=raw.get("last_npi"),
            count=int(raw.get("count", 0) or 0),
            total_count=int(raw.get("total_count", 0) or 0),
            page_start=int(raw.get("page_start", 1) or 1),
            page_end=int(raw.get("page_end", 0) or 0),
            search_params=raw.get("search_params"),
            specialization_options=raw.get("specialization_options"),
            state=raw.get("state") or request.state,
        )
        deps.stream({"kind": "providers", "data": resp.model_dump(exclude_none=True)})
        return resp


TOOL = ProviderSearchAndSelectionTool()
