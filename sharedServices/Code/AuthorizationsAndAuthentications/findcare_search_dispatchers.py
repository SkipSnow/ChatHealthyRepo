# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""FindCare search dispatchers, and the painting they and the gestures share.

Design V2 §2.4 / Designed change C-18: the three search actions --
specialtySearch, findAProvider, findAFacility -- dispatch the tool named for
them, exactly as safetyLockout, closeConnection200 and findClinicalTrials
already do. SharedServices does not host these searches and does not read the
utterance that reaches them: each dispatcher carries the person's latest
utterance and the talk before it to the FindCare page that owns the search,
and paints what comes back onto the person's gate stream. What the utterance
means -- the complaint, the kind of place, the geography -- is the page's to
decide, on the far side of the call.

The painting is written once here and the navigator's gesture handlers
delegate to it, because a search and a page turn produce the same events and
two copies of the shaping are two things to keep in step.

Canonical *_tool.py exports per dispatcher: TOOL_NAME, Request, Response,
run().
"""
from __future__ import annotations

from typing import Optional

import httpx
from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
from pydantic import BaseModel, Field

from chathealthy_lib.authentication.agent_deps import (AgentDeps,
                                                      append_system_utterance)
from chathealthy_lib.authentication.chathealthy_tool import ChatHealthyTool
from chathealthy_lib.capability_contract import CapabilityContract

log = ChatHealthyLoggingService()

# The pages these dispatchers write across the wire. Wire-level page names,
# stable, and defined here so this module carries no import back to the
# navigator that imports it.
FACILITY = "facility"
INDIVIDUAL_PROVIDER = "individualProvider"

_FINDCARE_UNREACHABLE = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
    httpx.WriteTimeout, httpx.PoolTimeout, httpx.ReadError,
    httpx.WriteError, httpx.RemoteProtocolError, httpx.HTTPStatusError,
)


# --------------------------------------------------------------------------
# Talking to the pages
# --------------------------------------------------------------------------
async def post_page(deps: AgentDeps, page_route: str, utterance: str,
                    history: list, mode: str) -> dict:
    """Hand one turn's utterance and prior talk to the page that owns it and
    give back what it said. The mode names which page could not be reached,
    so a failure says which search did not run."""
    from authentication import provider_search_tool
    url = provider_search_tool.findcare_url() + page_route
    try:
        async with httpx.AsyncClient(timeout=None, verify=False) as client:
            r = await client.post(url, json={
                # The token this hop already holds, forwarded so FindCare
                # can verify the SharedServices signature.
                "session_token": deps.session_token.model_dump(mode="json"),
                "utterance": utterance,
                "history": history,
            })
            r.raise_for_status()
            return r.json() or {}
    except _FINDCARE_UNREACHABLE as exc:
        raise ChatHealthyException(
            mode=mode,
            component="findcare_search_dispatchers",
            message=f"FindCare {page_route} call failed: "
                    f"{type(exc).__name__}: {exc}",
            exception=exc,
        )


async def tell_page(deps: AgentDeps, path: str, body: dict) -> dict:
    """Say something to the page that owns it, and take its answer. The body
    carries page names and record identities and no parameter name: which of
    its own parameters a page changes on being told this is the page's
    business."""
    from authentication import provider_search_tool
    url = provider_search_tool.findcare_url() + path
    try:
        async with httpx.AsyncClient(timeout=None, verify=False) as client:
            r = await client.post(url, json={
                "session_token": deps.session_token.model_dump(mode="json"),
                **body,
            })
            r.raise_for_status()
            return r.json() or {}
    except _FINDCARE_UNREACHABLE as exc:
        raise ChatHealthyException(
            mode="page_unreachable",
            component="findcare_search_dispatchers",
            message=f"FindCare {path} call failed: "
                    f"{type(exc).__name__}: {exc}",
            exception=exc,
        )


async def tell_page_detail_opened(deps: AgentDeps, page: str,
                                  record_id: str) -> None:
    """This page is showing this record."""
    await tell_page(deps, "/page/detail-open",
                    {"page": page, "record_id": record_id})


async def tell_page_detail_closed(deps: AgentDeps, page: str) -> None:
    """This page has stopped showing a record."""
    await tell_page(deps, "/page/detail-close", {"page": page})


# --------------------------------------------------------------------------
# Painting what comes back
# --------------------------------------------------------------------------
def stream_facilities(deps: AgentDeps, raw: dict) -> None:
    """Put a set of organizations on the window. Every field is taken from
    the page's answer and none is interpreted."""
    facilities = (raw or {}).get("providers") or []
    if not facilities:
        return
    data = {
        "facilities": facilities,
        "has_more": bool(raw.get("has_more", False)),
        "has_previous": bool(raw.get("has_previous", False)),
        "first_npi": raw.get("first_npi"),
        "last_npi": raw.get("last_npi"),
        "count": int(raw.get("count", 0) or 0),
        "total_count": int(raw.get("total_count", 0) or 0),
        "page_start": int(raw.get("page_start", 1) or 1),
        "page_end": int(raw.get("page_end", 0) or 0),
        "search_params": raw.get("search_params"),
        "state": raw.get("state"),
        "summary_message": raw.get("summary_message"),
    }
    deps.stream({
        "kind": "facilities",
        "data": {name: value for name, value in data.items()
                 if value is not None},
    })


async def ask_what_the_page_needs(deps: AgentDeps, raw: dict) -> None:
    """Say what the page still needs, in the page's own words. A page that
    cannot run answers with a question rather than an empty window; the
    question is authored where the requirement is known. Streamed as a
    prompt, which is an answering kind, so the manufactured question does not
    also fire."""
    question = (raw or {}).get("refinement_question")
    if not question:
        return
    deps.stream({"kind": "prompt", "data": {"text": question}})
    append_system_utterance(deps.user_object, question)


async def write_position(deps: AgentDeps, page: str,
                         first_key, last_key) -> None:
    """Record the page in view as its first and last key."""
    from UserParameters import user_parameters_tool
    await user_parameters_tool.TOOL.run_and_log(
        deps,
        user_parameters_tool.Request(
            verb="set", page=page, name="position",
            value={"first": first_key or "", "last": last_key or ""},
            route="gateway", origin="deterministic",
        ),
    )


async def handle_provider_detail_close(deps: AgentDeps) -> None:
    """The detail is no longer on screen. Record that, and take the panel
    down. selected/open means 'a detail is open', not 'a detail was opened
    once' -- a parameter that only ever gets set turns a return to FindCare
    into a resurrection."""
    await tell_page_detail_closed(deps, INDIVIDUAL_PROVIDER)
    deps.stream({"kind": "provider_detail_close", "data": {"closed": True}})


async def reconcile_open_detail(deps: AgentDeps, providers: list) -> None:
    """A detail belongs to a provider in the list being presented. Paging,
    narrowing, or restoring to a different page all replace the list the
    detail came from; leaving the panel up then describes somebody the user
    cannot see, and it is the panel that is wrong, not the list."""
    npi = str(deps.user_object.userParameters.get(
        INDIVIDUAL_PROVIDER, "openNpi") or "").strip()
    if not npi:
        return
    presented = {str(p.get("npi") or "").strip() for p in (providers or [])}
    if npi in presented:
        return
    await handle_provider_detail_close(deps)


async def paint_specialty_search(deps: AgentDeps, raw: dict) -> None:
    """The NUCC page's answer: the panel of kinds of care giver, painted
    only when there are rows to paint. A run that matched nothing says
    nothing about the panel, so what the person is looking at stays."""
    specialties = raw.get("specialties") or []
    if specialties:
        deps.stream({
            "kind": "specialties",
            "data": {
                "is_facility": False,
                "specialties": specialties,
                "homeopathic_generalists": [],
                "selected_codes": raw.get("selected_codes") or [],
                "complaint": raw.get("complaint") or "",
                "all_codes": raw.get("all_codes") or [],
                "prescriber_codes": raw.get("prescriber_codes") or [],
                "homeopathic_codes": raw.get("homeopathic_codes") or [],
                "default_selected_codes":
                    raw.get("default_selected_codes") or [],
            },
        })


async def paint_provider_search(deps: AgentDeps, raw: dict) -> None:
    """The individual-provider page's answer: the specialty panel, the list
    of care givers, then the open-detail and page-needs reconciliations."""
    specialties = raw.get("offered_specialties") or []
    if specialties:
        deps.stream({
            "kind": "specialties",
            "data": {
                "is_facility": False,
                "specialties": specialties,
                "homeopathic_generalists": [],
                "selected_codes": raw.get("selected_specialty_codes") or [],
                "complaint": raw.get("complaint") or "",
                "all_codes": raw.get("all_codes") or [],
                "prescriber_codes": raw.get("prescriber_codes") or [],
                "homeopathic_codes": raw.get("homeopathic_codes") or [],
                "default_selected_codes":
                    raw.get("default_selected_codes") or [],
            },
        })
    providers = raw.get("providers") or []
    if providers:
        data = {
            "providers": providers,
            "has_more": bool(raw.get("has_more", False)),
            "has_previous": bool(raw.get("has_previous", False)),
            "first_npi": raw.get("first_npi"),
            "last_npi": raw.get("last_npi"),
            "count": int(raw.get("count", 0) or 0),
            "total_count": int(raw.get("total_count", 0) or 0),
            "page_start": int(raw.get("page_start", 1) or 1),
            "page_end": int(raw.get("page_end", 0) or 0),
            "search_params": raw.get("search_params"),
            "specialization_options": raw.get("specialization_options"),
            "state": raw.get("state"),
            "refinements": raw.get("refinements"),
            "summary_message": raw.get("summary_message"),
        }
        deps.stream({
            "kind": "providers",
            "data": {name: value for name, value in data.items()
                     if value is not None},
        })
    await reconcile_open_detail(deps, providers)
    await ask_what_the_page_needs(deps, raw)


async def paint_facility_search(deps: AgentDeps, raw: dict) -> None:
    """The facility page's answer: the classified criteria, the facility-type
    filter panel (same widget as the care-giver specialty panel, carrying
    filter_mode='facility'), the organizations, the recorded position, and
    the page-needs reconciliation."""
    mined = raw.get("mined") or {}
    deps.stream({
        "kind": "intent_classified",
        "data": {
            "action": "findAFacility",
            "criteria": (mined.get("facility_name")
                         or mined.get("facility_type")
                         or "facilities"),
        },
    })
    facility_types = raw.get("offered_facility_types") or []
    if facility_types:
        deps.stream({
            "kind": "specialties",
            "data": {
                "filter_mode": "facility",
                "is_facility": True,
                "specialties": facility_types,
                "homeopathic_generalists": [],
                "selected_codes": raw.get("selected_facility_codes") or [],
                "complaint": raw.get("facility_type") or "",
                "all_codes": raw.get("all_codes") or [],
                "ambulatory_codes": raw.get("ambulatory_codes") or [],
                "inpatient_codes": raw.get("inpatient_codes") or [],
                "psychiatric_codes": raw.get("psychiatric_codes") or [],
                "default_selected_codes":
                    raw.get("default_selected_codes") or [],
            },
        })
    stream_facilities(deps, raw)
    await write_position(deps, FACILITY,
                         raw.get("first_npi"), raw.get("last_npi"))
    await ask_what_the_page_needs(deps, raw)


# --------------------------------------------------------------------------
# The dispatchers
# --------------------------------------------------------------------------
class Request(BaseModel):
    utterance: str = Field(
        default="",
        description="What the person last said. The FindCare page reads it; "
                    "nothing on this side does.")
    history: list[dict] = Field(
        default_factory=list,
        description="The talk before that utterance, oldest first, so a "
                    "complaint or a place named on an earlier turn is still "
                    "there to be read.")


class Response(BaseModel):
    error: Optional[str] = None


class _FindCareSearchDispatcher:
    """Common shape mixin: carry the turn to a FindCare page and paint what
    comes back. The concrete dispatchers add ChatHealthyTool +
    CapabilityContract and set the page route, the unavailable-mode name, and
    the painter."""
    SUBSCRIPTIONS: list[str] = []
    MAY_CALL: list[str] = []
    Request = Request
    Response = Response

    PAGE_ROUTE: str = ""
    UNAVAILABLE_MODE: str = ""

    async def _paint(self, deps: AgentDeps, raw: dict) -> None:
        raise NotImplementedError

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        raw = await post_page(deps, self.PAGE_ROUTE, request.utterance,
                              request.history, self.UNAVAILABLE_MODE)
        await self._paint(deps, raw)
        return Response()


class SpecialtySearchDispatcher(_FindCareSearchDispatcher, ChatHealthyTool,
                               CapabilityContract):
    TOOL_NAME = "specialty_search_dispatcher"
    CAPABILITY = "specialty_search_dispatcher"
    TOOL_DESCRIPTION = (
        "Carries the person's latest utterance and prior dialogue to the "
        "FindCare NUCC page and paints the specialty panel it returns.")
    UTTERANCE_MANAGER_PROMPT = (
        "Route here (specialtySearch) when the person is choosing the kind "
        "of care giver while their geography is not yet usable.")
    PAGE_ROUTE = "/specialty/find"
    UNAVAILABLE_MODE = "specialty_search_unavailable"

    async def _paint(self, deps, raw):
        await paint_specialty_search(deps, raw)


class ProviderSearchDispatcher(_FindCareSearchDispatcher, ChatHealthyTool,
                              CapabilityContract):
    TOOL_NAME = "provider_search_dispatcher"
    CAPABILITY = "provider_search_dispatcher"
    TOOL_DESCRIPTION = (
        "Carries the person's latest utterance and prior dialogue to the "
        "FindCare individual-provider page and paints the specialty panel "
        "and the care givers it returns.")
    UTTERANCE_MANAGER_PROMPT = (
        "Route here (findAProvider) when the person is looking for an "
        "individual care giver.")
    PAGE_ROUTE = "/provider/find"
    UNAVAILABLE_MODE = "provider_search_unavailable"

    async def _paint(self, deps, raw):
        await paint_provider_search(deps, raw)


class FacilitySearchDispatcher(_FindCareSearchDispatcher, ChatHealthyTool,
                              CapabilityContract):
    TOOL_NAME = "facility_search_dispatcher"
    CAPABILITY = "facility_search_dispatcher"
    TOOL_DESCRIPTION = (
        "Carries the person's latest utterance and prior dialogue to the "
        "FindCare facility page and paints the facility-type panel and the "
        "organizations it returns.")
    UTTERANCE_MANAGER_PROMPT = (
        "Route here (findAFacility) when the person is looking for a place "
        "that delivers care rather than an individual.")
    PAGE_ROUTE = "/facility/find"
    UNAVAILABLE_MODE = "facility_search_unavailable"

    async def _paint(self, deps, raw):
        await paint_facility_search(deps, raw)


SPECIALTY_SEARCH_TOOL = SpecialtySearchDispatcher()
PROVIDER_SEARCH_TOOL = ProviderSearchDispatcher()
FACILITY_SEARCH_TOOL = FacilitySearchDispatcher()
