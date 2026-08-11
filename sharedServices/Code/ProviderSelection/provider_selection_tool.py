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


MAX_SELECTED = 5


class Request(BaseModel):
    verb: Literal["select", "deselect", "list"]
    npi: Optional[str] = None


class Response(BaseModel):
    selected: list[str] = Field(default_factory=list)
    max_selected: int = MAX_SELECTED
    full: bool = False
    error: Optional[str] = None


class ProviderSelectionTool(ChatHealthyTool):
    TOOL_NAME = "provider_selection"
    Request = Request
    Response = Response

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
            error=error,
        )
        deps.stream({"kind": "selection_changed", "data": resp.model_dump(exclude_none=True)})
        return resp


TOOL = ProviderSelectionTool()
