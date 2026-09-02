# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""ProviderDetail tool wrapper — SharedServices side.

Receives a click-path payload from the UniversalNavigation tool router
(op == 'provider-detail'), HTTPS-hops into FindCare's /provider-detail
endpoint, and returns the structured detail JSON to the orchestrator.
No LLM. No utterance manager involvement — this is the click path.

EPIC-006-F-002 'Provider Detail'.
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
    FindCare's ProviderDetailInput so the body forwards verbatim.

    Only the NPI identifies anybody. name is optional because a detail is
    also opened from a recorded selection, which carries the NPI and no
    card; FindCare reads the name from the record in that case.
    """
    name: Optional[str] = Field(
        default=None,
        description="Display name from the card. Optional: a detail is also "
                    "opened from a recorded selection carrying only the NPI, "
                    "and the record supplies the name in that case.")
    npi: str = Field(
        description="The provider to open. The only field that identifies "
                    "anybody; the rest are the card's copy of what the record "
                    "already holds.")
    specialty: Optional[str] = Field(default=None, description="Card echo.")
    address: Optional[str] = Field(default=None, description="Card echo.")
    phone: Optional[str] = Field(default=None, description="Card echo.")
    entity_type: str = Field(
        default="1",
        description="Which page opened this detail: 1 for the "
                    "individual-provider page, 2 for the facility page. A "
                    "property of the page, not a filter the caller chooses.")
    state: Optional[str] = Field(
        default=None,
        description="Two-letter USPS code, used only to build external "
                    "research links when the record carries no practice "
                    "state.")


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
    """One payer identifier the provider carries. Four fields, never
    presented as coverage accepted or as network membership."""
    coverage_kind: str = ""
    issuer: str = ""
    state: str = ""
    identifier: str = ""


class TaxonomyRow(BaseModel):
    code: str = ""
    display_name: str = ""
    primary: bool = False


class OrganizationIdentityRow(BaseModel):
    """The identity a facility shows in place of a person's. No tax
    identifier is declared here, which is the transmission-side half of
    EPIC-006-F-007-S-001-REQ-B-005."""
    legal_business_name: str = ""
    other_organization_name: str = ""
    other_organization_name_kind: str = ""
    npi: str = ""
    enumeration_date: str = ""
    status_active: bool = True
    is_subpart: bool = False
    parent_organization_name: str = ""


class AuthorizedOfficialRow(BaseModel):
    """The person the registry recorded as the facility's authorized
    official. Five fields; the telephone number the record also carries is
    deliberately not among them."""
    last_name: str = ""
    first_name: str = ""
    middle_name: str = ""
    title_or_position: str = ""
    credential: str = ""


class FacilityKindRow(BaseModel):
    code: str = ""
    classification: str = ""
    specialization: str = ""
    definition: str = ""


class Response(BaseModel):
    provider_name: str = ""
    npi: str = ""
    # Which panel this is. The renderer selects the identity block from
    # this rather than from the shape of the data.
    entity_type: str = "1"
    organization_identity: Optional[OrganizationIdentityRow] = None
    authorized_official: Optional[AuthorizedOfficialRow] = None
    facility_kinds: list[FacilityKindRow] = Field(default_factory=list)
    identity: Optional[IdentityRow] = None
    addresses: list[AddressRow] = Field(default_factory=list)
    licenses: list[LicenseRow] = Field(default_factory=list)
    insurance: list[InsuranceRow] = Field(default_factory=list)
    taxonomies: list[TaxonomyRow] = Field(default_factory=list)
    research_sites: dict[str, ResearchSiteRow] = Field(default_factory=dict)
    # The practice state for which no licensing authority resolved, emitted
    # in place of the destination so a gap in the table surfaces.
    unresolved_licensing_state: str = ""
    error: Optional[str] = None


def findcare_url() -> str:
    return os.environ.get(FINDCARE_INTERNAL_URL_ENV) or FINDCARE_INTERNAL_URL_DEFAULT


class ProviderDetailTool(ChatHealthyTool):
    TOOL_NAME = "provider_detail"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        body: dict[str, Any] = request.model_dump(exclude_none=True)
        # The token this hop already holds, forwarded so FindCare can
        # verify the SharedServices signature on it.
        body["session_token"] = deps.session_token.model_dump(mode="json")
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
