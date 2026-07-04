# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""ClinicalTrialSelection tool — owns user_object.selected_clinical_trials.

Mirrors ProviderSelection. State (the list of NCT IDs the user has curated
for EvaluateCare handoff) lives on user_object so it survives page refresh.
The tool exposes three verbs: select, deselect, list. Each broadcasts
kind:'trial_selection_changed' with the current list so the
SelectedClinicalTrialsWidget repaints its strip.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from chathealthy_frontend_lib.authentication.agent_deps import AgentDeps
from chathealthy_frontend_lib.authentication.chathealthy_tool import ChatHealthyTool


MAX_SELECTED = 5


class Request(BaseModel):
    verb: Literal["select", "deselect", "list"]
    nct_id: Optional[str] = None


class Response(BaseModel):
    selected: list[str] = Field(default_factory=list)
    max_selected: int = MAX_SELECTED
    full: bool = False
    error: Optional[str] = None


class ClinicalTrialSelectionTool(ChatHealthyTool):
    TOOL_NAME = "clinical_trial_selection"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        user_obj = deps.user_object
        current = list(user_obj.selected_clinical_trials or [])
        error: Optional[str] = None

        if request.verb == "select":
            nct = (request.nct_id or "").strip()
            if not nct:
                error = "nct_id required for select"
            elif nct in current:
                pass
            elif len(current) >= MAX_SELECTED:
                error = f"selection full (max {MAX_SELECTED})"
            else:
                current.append(nct)

        elif request.verb == "deselect":
            nct = (request.nct_id or "").strip()
            if not nct:
                error = "nct_id required for deselect"
            else:
                current = [n for n in current if n != nct]

        user_obj.selected_clinical_trials = current

        resp = Response(
            selected=current,
            max_selected=MAX_SELECTED,
            full=len(current) >= MAX_SELECTED,
            error=error,
        )
        deps.stream({"kind": "trial_selection_changed", "data": resp.model_dump(exclude_none=True)})
        return resp


TOOL = ClinicalTrialSelectionTool()
