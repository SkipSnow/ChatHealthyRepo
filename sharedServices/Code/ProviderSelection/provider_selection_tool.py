# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""ProviderSelection tool — owns user_object.selected_providers.

State (the list of NPIs the user has curated for EvaluateCare handoff)
lives on user_object so it survives page refresh. The tool exposes three
verbs: select, deselect, list. Each broadcasts kind:'selection_changed'
with the current list so the SelectedProvidersWidget repaints its strip.
"""
from __future__ import annotations

import os

from typing import Literal, Optional

import httpx
from pydantic import BaseModel, Field

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.authentication.agent_deps import AgentDeps
from chathealthy_lib.authentication.chathealthy_tool import ChatHealthyTool

log = ChatHealthyLoggingService()




# FindCare owns the provider collection; this component owns the session.
# A component holding identities asks the owner for those records rather
# than opening the collection itself (C-26). The peer call is the one
# FacilitySearch already makes.
FINDCARE_INTERNAL_URL_ENV = "CH_INTERNAL_PEER_URL_FINDCARE"
FINDCARE_INTERNAL_URL_DEFAULT = "https://localhost:7860"


def _findcare_url() -> str:
    return (os.environ.get(FINDCARE_INTERNAL_URL_ENV)
            or FINDCARE_INTERNAL_URL_DEFAULT)


async def _records_for(deps, npis: list, entity_type: str) -> list:
    """The records FindCare holds for these identities, or none.

    A failure here costs the exclusion marks, not the selection: the person
    keeps what they chose and the strip is drawn without the flag rather
    than the turn dying, which is what happened when this opened the
    collection from the wrong component.
    """
    wanted = [n for n in (npis or []) if n]
    if not wanted:
        return []
    body = {
        "entity_type": entity_type,
        "npis": wanted,
        "limit": len(wanted),
        "session_token": deps.session_token.model_dump(mode="json"),
    }
    try:
        async with httpx.AsyncClient(timeout=None, verify=False) as client:
            r = await client.post(_findcare_url() + "/search", json=body)
            r.raise_for_status()
            return (r.json() or {}).get("providers") or []
    except Exception as exc:
        log.error("FindCare lookup by identity failed for %d record(s): %s",
                  len(wanted), exc,
                  exc=ChatHealthyException(
                      mode="findcare_identity_lookup_failed",
                      message=f"FindCare lookup by identity failed: {exc}",
                      component="SharedServices",
                      exception=exc if isinstance(exc, Exception) else None,
                  ))
        return []


MAX_SELECTED = 5


class Request(BaseModel):
    verb: Literal["select", "deselect", "list"] = Field(
        description="Add a provider to the evaluation set, remove one, or "
                    "return the set.")
    npi: Optional[str] = Field(
        default=None,
        description="Which provider. Required for select and deselect.")


class Response(BaseModel):
    selected: list[str] = Field(default_factory=list)
    max_selected: int = MAX_SELECTED
    full: bool = False
    # Per selected NPI: whether the specialty filter in force excludes that
    # provider (EPIC-006-F-001-S-002-REQ-B-019). The row is kept and marked,
    # never dropped -- a filter that silently discards a person's own choice
    # is the failure this exists to prevent.
    excluded_by_filter: dict[str, bool] = Field(default_factory=dict)
    error: Optional[str] = None


class ProviderSelectionTool(ChatHealthyTool):
    TOOL_NAME = "provider_selection"
    Request = Request
    Response = Response

    async def _excluded_by_filter(self, deps, selected: list[str],
                                  chosen_codes: list[str]) -> dict[str, bool]:
        """Which selected providers the chosen specialties do not admit.

        The selected set and the chosen codes are both parameters, so the
        exclusion is computable without re-running the search: a selected
        provider is excluded when none of its taxonomies is in the chosen
        set. Only the server holds the selected provider's full taxonomy
        list, which is why the flag is computed here and not in the browser.
        """
        if not selected:
            return {}
        chosen = set(chosen_codes or [])
        if not chosen:
            return {npi: False for npi in selected}
        held = {npi: set() for npi in selected}
        for row in await _records_for(deps, selected, "1"):
            npi = row.get("npi")
            if npi in held:
                held[npi] = {c for c in (row.get("taxonomy_codes") or []) if c}
        return {npi: not (held[npi] & chosen) for npi in selected}

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        user_obj = deps.user_object
        current = list(user_obj.selected_providers or [])
        error: Optional[str] = None

        if request.verb == "select":
            npi = (request.npi or "").strip()
            if not npi:
                error = "npi required for select"
            elif npi in current:
                pass
            elif len(current) >= MAX_SELECTED:
                error = f"selection full (max {MAX_SELECTED})"
            else:
                current.append(npi)

        elif request.verb == "deselect":
            npi = (request.npi or "").strip()
            if not npi:
                error = "npi required for deselect"
            else:
                current = [n for n in current if n != npi]

        user_obj.selected_providers = current

        resp = Response(
            selected=current,
            max_selected=MAX_SELECTED,
            full=len(current) >= MAX_SELECTED,
            excluded_by_filter=await self._excluded_by_filter(
                deps, current,
                user_obj.userParameters.get(
                    "individualProvider", "selectedSpecialtyCodes") or []),
            error=error,
        )
        deps.stream({"kind": "selection_changed", "data": resp.model_dump(exclude_none=True)})
        return resp


TOOL = ProviderSelectionTool()
