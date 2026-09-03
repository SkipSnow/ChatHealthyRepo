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
    async def _rows(deps, selected: list[str]) -> list[dict]:
        """Exactly the legal business name and the NPI, per row."""
        if not selected:
            return []
        by_npi = {}
        for row in await _records_for(deps, selected, "2"):
            by_npi[row.get("npi")] = row.get("name") or ""
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
        resp = Response(selected=current, rows=await self._rows(deps, current), error=error)
        deps.stream({"kind": "facility_selection_changed",
                     "data": resp.model_dump(exclude_none=True)})
        return resp


TOOL = FacilitySelectionTool()
