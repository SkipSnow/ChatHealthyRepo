# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""ProviderDetail tool wrapper — SharedServices side.

Receives a click-path payload from the UniversalNavigation tool router
(op == 'provider-detail'), HTTPS-hops into FindCare's /provider-detail
endpoint, and returns the structured detail JSON to the orchestrator.
No LLM. No utterance manager involvement — this is the click path.

EPIC-006-F-025 'Provider Detail'.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

from authentication.agent_deps import AgentDeps
from authentication.chathealthy_tool import ChatHealthyTool

_log = logging.getLogger("shared_services.provider_detail")

_FINDCARE_INTERNAL_URL_ENV = "FINDCARE_INTERNAL_URL"
_FINDCARE_INTERNAL_URL_DEFAULT = "https://ch-findcare:7860"


class Request(BaseModel):
    """Mirrors the on-screen provider-card fields. Same shape as
    FindCare's ProviderDetailInput so the body forwards verbatim."""
    name: str
    npi: str
    specialty: Optional[str] = None
    address: Optional[str] = None
    county: Optional[str] = None
    phone: Optional[str] = None
    state: Optional[str] = None


class ResearchSiteRow(BaseModel):
    url: str
    name: str
    guidance: str


class NpiDetailsRow(BaseModel):
    name: str
    npi: str
    status: str
    credential: str
    specialty: str
    license: str
    license_state: str
    address: str
    phone: str
    enumeration_date: str


class Response(BaseModel):
    provider_name: str = ""
    npi: str = ""
    npi_details: Optional[NpiDetailsRow] = None
    research_sites: dict[str, ResearchSiteRow] = Field(default_factory=dict)
    error: Optional[str] = None


def _findcare_url() -> str:
    return os.environ.get(_FINDCARE_INTERNAL_URL_ENV) or _FINDCARE_INTERNAL_URL_DEFAULT


class ProviderDetailTool(ChatHealthyTool):
    TOOL_NAME = "provider_detail"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        body: dict[str, Any] = request.model_dump(exclude_none=True)
        url = _findcare_url() + "/provider-detail"
        try:
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                r = await client.post(url, json=body)
                r.raise_for_status()
                raw = r.json()
        except Exception as exc:
            _log.error("provider_detail HTTP /provider-detail failed: %s: %s",
                       type(exc).__name__, exc)
            resp = self.Response(error=f"detail_unavailable: {type(exc).__name__}")
            deps.stream({"kind": "provider-detail", "data": resp.model_dump(exclude_none=True)})
            return resp

        resp = self.Response(**raw)
        deps.stream({"kind": "provider-detail", "data": resp.model_dump(exclude_none=True)})
        return resp


TOOL = ProviderDetailTool()
