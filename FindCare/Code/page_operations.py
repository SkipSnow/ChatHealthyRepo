# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""FindCare page operations, drained out of app.py.

The list and gesture operations more than one page shares: paging a result
list, asking what a page still needs, recording an open/closed detail, and
marking which care givers the filter in force admits. app.py's /search,
/page/what-is-needed, /page/detail-open, /page/detail-close and
/provider/exclusions routes collect the request, prove the gateway signature,
and hand the turn here.
"""
from __future__ import annotations

import asyncio

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.runtime_data_collections import declared_attributes

from db_config import INDIVIDUAL_PROVIDER_PAGE
from page_parameters import write_page_parameters
from route_support import (unmet_requirements, question_for,
                           read_page_parameter, clear_page_parameter,
                           open_record_attribute, gesture_entry)
from services import find_care

log = ChatHealthyLoggingService()


async def search(params: dict) -> dict:
    """Direct provider search -- for pagination. No LLM involved."""
    try:
        return await asyncio.to_thread(
            lambda: find_care.search_providers(**params))
    except ChatHealthyException as exc:
        if exc.mode == "mongo_query_timeout":
            log.error("search Mode 2: mongo_query_timeout on %s.%s",
                      exc.context.get("db"), exc.context.get("coll"),
                      exc=exc, if_not_debug_log=True)
            return {
                "providers": [],
                "total_count": 0,
                "error": "Provider search is taking longer than usual. "
                         "Please try the same search again in a moment.",
                "error_mode": exc.mode,
            }
        raise


async def what_is_needed(page: str, tool: str, utterance: str,
                         history: list) -> dict:
    """What this page still needs, and the question that asks for it. Read
    from the session rather than from the turn, because the turn may have
    written nothing -- what matters is what the page has."""
    def _in_force() -> dict:
        return {name: read_page_parameter(page, name)
                for name in declared_attributes(page)}

    in_force = await asyncio.to_thread(_in_force)
    unmet = unmet_requirements(tool, in_force)
    if not unmet:
        return {"unmet_requirements": [], "refinement_question": ""}
    return {
        "unmet_requirements": unmet,
        "refinement_question": await question_for(
            tool, unmet, in_force, utterance, history),
    }


async def detail_open(page: str, record_id: str) -> dict:
    """Record that this page is showing this record, so a return puts the
    person back on it rather than at the top of the list."""
    record = (record_id or "").strip()
    if not record:
        return {"opened": False}
    await asyncio.to_thread(
        write_page_parameters, page,
        {open_record_attribute(page): gesture_entry(record)})
    return {"opened": True}


async def detail_close(page: str) -> dict:
    """Record that this page has stopped showing a record. Not clearing it is
    what resurrects a panel on the next return, so the close is a write."""
    await asyncio.to_thread(clear_page_parameter, page,
                            open_record_attribute(page))
    return {"closed": True}


async def provider_exclusions(npis: list[str]) -> dict:
    """Mark, per identity, whether the specialties in force admit them. The row
    is marked and kept, never dropped (EPIC-006-F-001-S-002-REQ-B-019). Only
    this page holds a care giver's full taxonomy list, so the comparison is
    made here."""
    wanted = [npi for npi in (npis or []) if npi]
    if not wanted:
        return {"excluded": {}}
    chosen = set(await asyncio.to_thread(
        read_page_parameter, INDIVIDUAL_PROVIDER_PAGE,
        "selectedSpecialtyCodes") or [])
    if not chosen:
        return {"excluded": {npi: False for npi in wanted}}
    found = await asyncio.to_thread(
        lambda: find_care.search_providers(
            entity_type="1", npis=wanted, limit=len(wanted)))
    held = {npi: set() for npi in wanted}
    for row in (found or {}).get("providers") or []:
        npi = row.get("npi")
        if npi in held:
            held[npi] = {code for code in (row.get("taxonomy_codes") or [])
                         if code}
    return {"excluded": {npi: not (held[npi] & chosen) for npi in wanted}}
