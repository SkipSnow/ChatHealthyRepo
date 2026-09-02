# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""ProviderSelection tool — owns user_object.selected_providers.

State (the list of NPIs the user has curated for EvaluateCare handoff)
lives on user_object so it survives page refresh. The tool exposes three
verbs: select, deselect, list. Each broadcasts kind:'selection_changed'
with the current list so the SelectedProvidersWidget repaints its strip.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from chathealthy_lib.authentication.agent_deps import AgentDeps
from chathealthy_lib.authentication.chathealthy_tool import ChatHealthyTool
from chathealthy_lib.runtime_data_collections import providers_coll


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

    def _excluded_by_filter(self, selected: list[str],
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
        held: dict[str, set] = {npi: set() for npi in selected}
        for doc in providers_coll().find(
                {"npi": {"$in": selected}}, {"_id": 0, "npi": 1, "taxonomies": 1}):
            npi = doc.get("npi")
            if npi in held:
                held[npi] = {t.get("code") for t in (doc.get("taxonomies") or [])
                             if isinstance(t, dict) and t.get("code")}
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
            excluded_by_filter=self._excluded_by_filter(
                current,
                user_obj.userParameters.get(
                    "individualProvider", "selectedSpecialtyCodes") or []),
            error=error,
        )
        deps.stream({"kind": "selection_changed", "data": resp.model_dump(exclude_none=True)})
        return resp


TOOL = ProviderSelectionTool()
