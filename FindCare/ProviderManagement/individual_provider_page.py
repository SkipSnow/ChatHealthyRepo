# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""The individual-provider page handler, drained out of app.py.

app.py's /provider/find route collects the request, proves the gateway
signature, and hands the turn here. This owns the page's whole turn: mine its
parameters, let SpecialtyFilter decide cache-vs-LLM on the complaint, search on
what is in force, and answer with the panel + results + any refinement.
"""
from __future__ import annotations

import asyncio

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

from db_config import INDIVIDUAL_PROVIDER_PAGE, PROVIDER_SEARCH_TOOL
from page_parameters import (parameter_entry, parameters_in_force,
                             write_page_parameters)
from services import resolve_specialties, ticked, specialty_groups, find_care
from route_support import (unmet_requirements, geography_known, question_for,
                           searched_codes, sex_code, provider_page_entries)
from ProviderManagement.individual_provider_utterance import (
    mine_individual_provider_parameters)

log = ChatHealthyLoggingService()


async def find(utterance: str, history: list) -> dict:
    """The individual-provider page mines its own parameters and searches on
    them. Local catch with mode discrimination per EPIC-008-F-002-S-009-REQ-B-008."""
    try:
        mined = await mine_individual_provider_parameters(utterance, history)
        in_force = await asyncio.to_thread(parameters_in_force,
                                           INDIVIDUAL_PROVIDER_PAGE)
        prior_complaint = str(in_force.get("complaint") or "")
        prior_offered = list(in_force.get("offeredSpecialties") or [])
        if mined.complaint:
            # SpecialtyFilter owns the cache-vs-LLM decision: given the prior
            # complaint and the specialties already resolved for it, its model
            # -- not this handler -- decides whether the request materially
            # changed. This handler never compares complaints.
            resolved = await resolve_specialties(
                mined.complaint, prior_complaint=prior_complaint,
                cached=prior_offered)
            offered = resolved["specialties"]
            complaint = resolved["complaint"]
            if resolved.get("reused"):
                chosen = list(in_force.get("selectedSpecialtyCodes") or [])
            else:
                chosen = ticked(offered)
        else:
            # No complaint this turn -> the standing specialties hold.
            offered = prior_offered
            complaint = prior_complaint
            chosen = list(in_force.get("selectedSpecialtyCodes") or [])
        codes = searched_codes(offered, chosen)
        # Persist the mined non-specialty values every turn (so "no, NY"
        # merges), plus the specialties in force now.
        entries = provider_page_entries(mined, complaint, chosen)
        entries["offeredSpecialties"] = parameter_entry(offered)
        await asyncio.to_thread(
            write_page_parameters, INDIVIDUAL_PROVIDER_PAGE, entries)
        in_force = await asyncio.to_thread(parameters_in_force,
                                           INDIVIDUAL_PROVIDER_PAGE)
        name = in_force.get("providerName") or {}
        result: dict = {"providers": [], "total_count": 0}
        known = await asyncio.to_thread(geography_known, in_force)
        unmet = unmet_requirements(PROVIDER_SEARCH_TOOL, known)
        if not unmet:
            result = await asyncio.to_thread(lambda: find_care.search_providers(
                entity_type="1",
                nucc_codes=codes,
                state=str(in_force.get("state") or ""),
                city=str(in_force.get("city") or ""),
                county=str(in_force.get("county") or ""),
                zip=str(in_force.get("zip") or ""),
                last_name=str(name.get("last") or "").strip().upper(),
                first_name=str(name.get("first") or "").strip().upper(),
                middle_name=str(name.get("middle") or "").strip().upper(),
                provider_sex=sex_code(str(in_force.get("providerSex") or "")),
                sole_proprietor=in_force.get("soleProprietor"),
                insurance=str(in_force.get("insurance") or ""),
            ))
        result["mined"] = mined.model_dump()
        result["unmet_requirements"] = unmet
        result["complaint"] = complaint
        result["offered_specialties"] = offered
        result["selected_specialty_codes"] = chosen
        result.update(specialty_groups(offered))
        if unmet:
            question = (await question_for(
                PROVIDER_SEARCH_TOOL, unmet, known, utterance, history)
                or "").strip()
            if not question:
                # The model authored no question. The turn MUST NOT end silent;
                # name what is missing so the person can answer. Honest-error
                # path, not a fallback: the failure is surfaced, not hidden.
                question = ("I'm sorry -- I couldn't phrase that just now. "
                            f"Could you tell me your {', '.join(unmet)} so I "
                            f"can run the search?")
            result["refinement_question"] = question
        return result
    except ChatHealthyException as exc:
        if exc.mode == "mongo_query_timeout":
            # Mode 2 (REQ-B-008): resource temporarily unavailable. Graceful
            # user-facing 200 carrying an error string; NOT 503.
            log.error("provider_find Mode 2: mongo_query_timeout on %s.%s",
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
