# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""The facility page handlers, drained out of app.py.

app.py's /facility/find and /facility/page routes collect the request, prove
the gateway signature, and hand the turn here. This owns the facility page:
resolve the kind of place, search on what is in force, page the list, and
answer with the facility-type panel + results + any refinement.
"""
from __future__ import annotations

import asyncio

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.authentication.session_token import SessionToken

from db_config import (get_db, SESSION_DB, SESSION_COLLECTION, FACILITY_PAGE,
                       FACILITY_SEARCH_TOOL)
from page_parameters import parameters_in_force, write_page_parameters
from route_support import (geography_known, unmet_requirements, question_for,
                           read_page_parameter, gesture_entry)
from services import find_care, specialty_service
from SpecialtyFilter.filter import SECTION_ORGANIZATION, facility_groups
from ProviderManagement.facility_utterance import mine_facility_parameters

log = ChatHealthyLoggingService()


async def _facility_kinds(facility_type: str) -> list[dict]:
    """The kinds of place a facility type resolves to. find_specialties is its
    own single catch point; an unresolved kind of place is not a search across
    every organization, so it raises rather than searching wide."""
    resolved = await specialty_service.find_specialties(
        facility_type, None, SECTION_ORGANIZATION)
    if "error" in resolved:
        raise ChatHealthyException(
            mode="facility_type_unresolved",
            component="FindCareBackend",
            message=f"facility type {facility_type!r} did not resolve: "
                    f"{resolved['error']}")
    return [{"code": row["Code"], "name": row["Display Name"],
             "can_prescribe": row.get("can_prescribe", False),
             "homeopathic": row.get("homeopathic", False),
             "grouping": row.get("Grouping", ""),
             "rank": row.get("rank", 0)}
            for row in resolved.get("specialties", [])]


def _facility_page_entries(mined, offered: list[dict],
                           codes: list[str]) -> dict:
    """The mined values as parameter entries, keyed by attribute."""
    from chathealthy_lib.authentication.user_parameters import ParameterEntry

    def entry(value):
        return ParameterEntry(value=value, route="tool",
                              determination="model").model_dump(
                                  exclude_none=True)

    entries: dict = {}
    for part, value in mined.geography.model_dump().items():
        if part in ("state", "city", "zip", "county") and value:
            entries[part] = entry(value)
    if mined.facility_type:
        entries["facilityType"] = entry(mined.facility_type)
    if mined.facility_name:
        entries["facilityName"] = entry(mined.facility_name)
    if codes:
        entries["offeredFacilityTypes"] = entry(offered)
        entries["selectedTaxonomyCodes"] = entry(codes)
    return entries


def _write_facility_parameters(session_token: dict, entries: dict) -> None:
    """The page that mined a parameter writes it, on its own page."""
    if not entries:
        return
    db = get_db()
    if db is None:
        raise ChatHealthyException(
            mode="mongo_network_failure",
            component="FindCareBackend",
            message="facility parameters mined but the session is "
                    "unreachable to write them to")
    guid = SessionToken.model_validate(session_token).session_guid()
    result = db[SESSION_DB][SESSION_COLLECTION].update_one(
        {"_id": guid},
        {"$set": {f"userParameters.pages.{FACILITY_PAGE}.{name}": value
                  for name, value in entries.items()}})
    if result.matched_count == 0:
        raise ChatHealthyException(
            mode="session_not_found",
            component="FindCareBackend",
            message=f"no session {guid!r} to write the mined facility "
                    f"parameters to")


async def find(session_token: dict, utterance: str, history: list) -> dict:
    """The facility page mines its own parameters and searches on them."""
    try:
        mined = await mine_facility_parameters(utterance, history)
        offered: list[dict] = []
        if mined.facility_type:
            offered = await _facility_kinds(mined.facility_type)
        codes = [row["code"] for row in offered]
        await asyncio.to_thread(
            _write_facility_parameters, session_token,
            _facility_page_entries(mined, offered, codes))
        in_force = await asyncio.to_thread(parameters_in_force, FACILITY_PAGE)
        if not codes:
            codes = list(in_force.get("selectedTaxonomyCodes") or [])
        result: dict = {"providers": [], "total_count": 0}
        known = await asyncio.to_thread(geography_known, in_force)
        unmet = unmet_requirements(FACILITY_SEARCH_TOOL, known)
        if not unmet:
            result = await asyncio.to_thread(lambda: find_care.search_providers(
                entity_type="2",
                nucc_codes=codes,
                state=str(in_force.get("state") or ""),
                city=str(in_force.get("city") or ""),
                county=str(in_force.get("county") or ""),
                zip=str(in_force.get("zip") or ""),
                facility_name=str(in_force.get("facilityName") or ""),
            ))
        result["mined"] = mined.model_dump()
        result["unmet_requirements"] = unmet
        if unmet:
            result["refinement_question"] = await question_for(
                FACILITY_SEARCH_TOOL, unmet, known, utterance, history)
        panel_offered = offered or list(
            in_force.get("offeredFacilityTypes") or [])
        if panel_offered:
            result["offered_facility_types"] = panel_offered
            result["selected_facility_codes"] = codes
            result["facility_type"] = (
                mined.facility_type or str(in_force.get("facilityType") or ""))
            result.update(facility_groups(panel_offered))
        return result
    except ChatHealthyException as exc:
        if exc.mode == "mongo_query_timeout":
            log.error("facility_find Mode 2: mongo_query_timeout on %s.%s",
                      exc.context.get("db"), exc.context.get("coll"),
                      exc=exc, if_not_debug_log=True)
            return {
                "providers": [],
                "total_count": 0,
                "error": "Facility search is taking longer than usual. "
                         "Please try the same search again in a moment.",
                "error_mode": exc.mode,
            }
        raise


async def page(cursor: str, direction: str, limit: int) -> dict:
    """Page the facility list on the parameters already in force. Keyset
    paging on this page's own recorded position."""
    if direction not in ("forward", "back"):
        raise ChatHealthyException(
            mode="value_error", component="FindCareBackend",
            message=f"unknown direction {direction!r}")
    if not (cursor or "").strip():
        raise ChatHealthyException(
            mode="value_error", component="FindCareBackend",
            message="a page of a list continues from somewhere, and no "
                    "cursor was given")

    def _in_force() -> dict:
        geo = {part: read_page_parameter(FACILITY_PAGE, part) or ""
               for part in ("state", "city", "zip", "county")}
        administrator = read_page_parameter(
            FACILITY_PAGE, "administratorName") or {}
        return {
            "state": geo.get("state") or "",
            "city": geo.get("city") or "",
            "county": geo.get("county") or "",
            "zip": geo.get("zip") or "",
            "facility_name": read_page_parameter(
                FACILITY_PAGE, "facilityName") or "",
            "administrator_last_name": administrator.get("last") or "",
            "administrator_first_name": administrator.get("first") or "",
            "administrator_middle_name": administrator.get("middle") or "",
            "nucc_codes": list(read_page_parameter(
                FACILITY_PAGE, "selectedTaxonomyCodes") or []),
        }

    in_force = await asyncio.to_thread(_in_force)
    result = await asyncio.to_thread(
        lambda: find_care.search_providers(
            entity_type="2", limit=limit, cursor=cursor,
            direction=direction, **in_force))
    await asyncio.to_thread(
        write_page_parameters, FACILITY_PAGE,
        {"position": gesture_entry(
            {"first": str(result.get("first_npi") or ""),
             "last": str(result.get("last_npi") or "")})})
    return result
