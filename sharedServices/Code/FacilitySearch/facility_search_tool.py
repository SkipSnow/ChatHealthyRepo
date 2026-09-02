# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""FacilitySearch tool — EPIC-006-F-006.

A facility is a legal entity where care is delivered. In the registry it
is an organization: the same record shape as a care giver, distinguished
by its entity type. This tool is the facility page's caller of the one
provider search, and it differs from the care-giver caller in exactly one
structural way -- the entity type it asks for.

That entity type is a property of the page, not a filter the caller may
pass and therefore may omit, so it is written here once and never taken
from a request.

Canonical *_tool.py exports: TOOL_NAME, Request, Response, run().
"""
from __future__ import annotations

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
import os
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

from chathealthy_lib.authentication.agent_deps import AgentDeps
from chathealthy_lib.authentication.chathealthy_tool import ChatHealthyTool

log = ChatHealthyLoggingService()


FINDCARE_INTERNAL_URL_ENV = "FINDCARE_INTERNAL_URL"
FINDCARE_INTERNAL_URL_DEFAULT = "https://ch-findcare:7860"

# The page this tool serves, and the entity type that page returns. Every
# record the facility page can return is an organization, so an entity
# type is not a parameter the page declares
# (EPIC-006-F-008-S-006-REQ-B-005) and not an argument this request
# carries.
PAGE = "facility"
PAGE_ENTITY_TYPE = "2"


class Request(BaseModel):
    facility_name: Optional[str] = Field(
        default=None,
        description="An organization named outright. Matched against its "
                    "legal business name or its other organization name -- "
                    "the registry holds two names for one organization and "
                    "matching either is matching.")
    administrator_last_name: Optional[str] = Field(
        default=None,
        description="Surname of the person who administers the facility, "
                    "as the registry's authorized official.")
    administrator_first_name: Optional[str] = Field(
        default=None, description="Given name or initial of that person.")
    administrator_middle_name: Optional[str] = Field(
        default=None, description="Middle name or initial of that person.")
    taxonomy_codes: list[str] = Field(
        default_factory=list,
        description="Kinds of facility. A facility matches when it carries "
                    "at least one of them, not all.")
    state: Optional[str] = Field(default=None)
    city: Optional[str] = Field(default=None)
    county: Optional[str] = Field(default=None)
    zip: Optional[str] = Field(default=None)
    limit: int = Field(default=25)
    cursor: Optional[str] = Field(
        default=None,
        description="Keyset position: the NPI this page is taken relative "
                    "to. Absent means the first page.")
    direction: str = Field(
        default="forward",
        description="forward for the facilities after the cursor, back for "
                    "those before it.")


class Response(BaseModel):
    facilities: list[dict] = Field(default_factory=list)
    has_more: bool = False
    first_npi: Optional[str] = None
    last_npi: Optional[str] = None
    count: int = 0
    total_count: int = 0
    page_start: int = 1
    page_end: int = 0
    search_params: Optional[dict] = None
    state: Optional[str] = None
    summary_message: Optional[str] = None
    error: Optional[str] = None


def findcare_url() -> str:
    return os.environ.get(FINDCARE_INTERNAL_URL_ENV) or FINDCARE_INTERNAL_URL_DEFAULT


class FacilitySearchTool(ChatHealthyTool):
    TOOL_NAME = "facility_search"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        body: dict[str, Any] = {
            "entity_type": PAGE_ENTITY_TYPE,
            # The token this hop already holds, forwarded so FindCare can
            # verify the SharedServices signature on it.
            "session_token": deps.session_token.model_dump(mode="json"),
            "limit": request.limit,
            "direction": request.direction,
        }
        for field in ("facility_name", "administrator_last_name",
                      "administrator_first_name", "administrator_middle_name",
                      "state", "city", "county", "zip", "cursor"):
            value = getattr(request, field, None)
            if value:
                body[field] = value
        if request.taxonomy_codes:
            body["nucc_codes"] = request.taxonomy_codes

        url = findcare_url() + "/search"
        try:
            async with httpx.AsyncClient(timeout=None, verify=False) as client:
                r = await client.post(url, json=body)
                r.raise_for_status()
                raw = r.json()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.WriteTimeout, httpx.PoolTimeout, httpx.ReadError,
                httpx.WriteError, httpx.RemoteProtocolError,
                httpx.HTTPStatusError) as exc:
            # Mode 2: FindCare /search temporarily unavailable. The tool
            # returns a graceful Response.error inline; NOT 503.
            log.error("FindCare /search call failed for facilities: %s: %s",
                      type(exc).__name__, exc,
                      exc=ChatHealthyException(
                          mode="facility_search_unavailable",
                          message=(f"FindCare /search call failed for "
                                   f"facilities: {type(exc).__name__}: {exc}"),
                          component="FacilitySearchTool",
                          exception=exc,
                      ), if_not_debug_log=True)
            resp = self.Response(
                error="Facility search is taking longer than usual. "
                      "Please try the same search again in a moment.")
            deps.stream({"kind": "facilities",
                         "data": resp.model_dump(exclude_none=True)})
            return resp

        resp = self.Response(
            facilities=raw.get("providers") or [],
            has_more=bool(raw.get("has_more", False)),
            first_npi=raw.get("first_npi"),
            last_npi=raw.get("last_npi"),
            count=int(raw.get("count", 0) or 0),
            total_count=int(raw.get("total_count", 0) or 0),
            page_start=int(raw.get("page_start", 1) or 1),
            page_end=int(raw.get("page_end", 0) or 0),
            search_params=raw.get("search_params"),
            state=raw.get("state") or request.state,
            summary_message=raw.get("summary_message"),
        )
        deps.stream({"kind": "facilities",
                     "data": resp.model_dump(exclude_none=True)})
        return resp


TOOL = FacilitySearchTool()
