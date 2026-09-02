# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""FacilitySelection tool — owns user_object.selected_facilities.

The care-giver selection with its own set: one server-side writer, the
same three verbs, persisted with the session so nothing in the browser
holds the selection and nothing in the browser can lose it.

A row of the selected set carries exactly the legal business name and the
NPI (EPIC-006-F-006-S-003-REQ-B-004) -- a narrower projection than the
result row, because the selected row is a reminder of what was chosen
rather than a second result row.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from chathealthy_lib.authentication.agent_deps import AgentDeps
from chathealthy_lib.authentication.chathealthy_tool import ChatHealthyTool
from chathealthy_lib.runtime_data_collections import providers_coll


class Request(BaseModel):
    verb: Literal["select", "deselect", "list"] = Field(
        description="Add a facility to the set, remove one, or return it.")
    npi: Optional[str] = Field(
        default=None,
        description="Which facility. Required for select and deselect; one "
                    "NPI removes one facility in a single action "
                    "(EPIC-006-F-006-S-003-REQ-B-003).")


class Response(BaseModel):
    selected: list[str] = Field(default_factory=list)
    rows: list[dict] = Field(default_factory=list)
    error: Optional[str] = None


class FacilitySelectionTool(ChatHealthyTool):
    TOOL_NAME = "facility_selection"
    Request = Request
    Response = Response

    @staticmethod
    def _rows(selected: list[str]) -> list[dict]:
        """Exactly the legal business name and the NPI, per row."""
        if not selected:
            return []
        by_npi = {}
        for doc in providers_coll().find(
                {"npi": {"$in": selected}},
                {"_id": 0, "npi": 1,
                 "provider_organization_name_legal_business_name": 1}):
            by_npi[doc.get("npi")] = doc.get(
                "provider_organization_name_legal_business_name") or ""
        return [{"facility": by_npi.get(npi, ""), "npi": npi}
                for npi in selected]

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        user_obj = deps.user_object
        current = list(user_obj.selected_facilities or [])
        error: Optional[str] = None

        if request.verb == "select":
            npi = (request.npi or "").strip()
            if not npi:
                error = "npi required for select"
            elif npi not in current:
                current.append(npi)
        elif request.verb == "deselect":
            npi = (request.npi or "").strip()
            if not npi:
                error = "npi required for deselect"
            else:
                current = [n for n in current if n != npi]

        user_obj.selected_facilities = current
        resp = Response(selected=current, rows=self._rows(current), error=error)
        deps.stream({"kind": "facility_selection_changed",
                     "data": resp.model_dump(exclude_none=True)})
        return resp


TOOL = FacilitySelectionTool()
