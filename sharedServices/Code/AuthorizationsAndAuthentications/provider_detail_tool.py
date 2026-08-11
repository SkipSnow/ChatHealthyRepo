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

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
import os
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field, HttpUrl

from chathealthy_lib.authentication.agent_deps import AgentDeps
from chathealthy_lib.authentication.chathealthy_tool import ChatHealthyTool

log = ChatHealthyLoggingService()


FINDCARE_INTERNAL_URL_ENV = "FINDCARE_INTERNAL_URL"
FINDCARE_INTERNAL_URL_DEFAULT = "https://ch-findcare:7860"


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
    url: HttpUrl
    name: str
    guidance: str


class IdentityRow(BaseModel):
    name_prefix: str = ""
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    credentials: str = ""
    npi: str = ""
    primary_taxonomy_display: str = ""
    status_active: bool = True
    enumeration_date: str = ""


class CountyRow(BaseModel):
    name: str = ""
    urban: Optional[bool] = None


class AddressRow(BaseModel):
    address_type: str = ""
    line1: str = ""
    line2: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    country: str = ""
    phone: str = ""
    county: Optional[CountyRow] = None


class LicenseRow(BaseModel):
    state: str = ""
    number: str = ""


class InsuranceRow(BaseModel):
    insurance_type: str = ""
    brand: str = ""
    state: str = ""
    raw_value: str = ""


class TaxonomyRow(BaseModel):
    code: str = ""
    display_name: str = ""
    primary: bool = False


class Response(BaseModel):
    provider_name: str = ""
    npi: str = ""
    identity: Optional[IdentityRow] = None
    addresses: list[AddressRow] = Field(default_factory=list)
    licenses: list[LicenseRow] = Field(default_factory=list)
    insurance: list[InsuranceRow] = Field(default_factory=list)
    taxonomies: list[TaxonomyRow] = Field(default_factory=list)
    research_sites: dict[str, ResearchSiteRow] = Field(default_factory=dict)
    error: Optional[str] = None


def findcare_url() -> str:
    return os.environ.get(FINDCARE_INTERNAL_URL_ENV) or FINDCARE_INTERNAL_URL_DEFAULT


class ProviderDetailTool(ChatHealthyTool):
    TOOL_NAME = "provider_detail"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        body: dict[str, Any] = request.model_dump(exclude_none=True)
        url = findcare_url() + "/provider-detail"
        try:
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                r = await client.post(url, json=body)
                r.raise_for_status()
                raw = r.json()
        except Exception as exc:
            # Mode 2 (REQ-B-008): FC /provider-detail temporarily unavailable;
            # tool returns graceful Response.error inline. NOT 503; no
            # fatal_error tag.
            log.error("provider_detail HTTP /provider-detail failed: %s: %s",
                       type(exc).__name__, exc,
                       exc=ChatHealthyException(
                        mode="provider_detail_unavailable",
                        message=f"provider_detail HTTP /provider-detail failed: {type(exc).__name__}: {exc}",
                        component="ProviderDetailTool",
                        exception=exc,
                    ), if_not_debug_log=True)
            resp = self.Response(error=f"detail_unavailable: {type(exc).__name__}")
            deps.stream({"kind": "provider-detail", "data": resp.model_dump(exclude_none=True, mode='json')})
            return resp

        resp = self.Response(**raw)
        deps.stream({"kind": "provider-detail", "data": resp.model_dump(exclude_none=True, mode='json')})
        return resp


TOOL = ProviderDetailTool()
