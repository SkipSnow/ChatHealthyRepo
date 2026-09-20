# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Geo tool — extracts a US location from an utterance and writes it onto
the page's own geography parameter.

Geography is page-scoped: the individualProvider page and the facility
page each hold their OWN geography — it is two parameters, not one — so
the tool must be told which page it is servicing (Request.page) and writes
onto that page and no other. It wraps the shared geo_extractor and, on a
non-empty extraction, sets the located parts on the named page through
UserParameters.

Fired in parallel with SpecialtyFilter: when both resolve, the page's
search has what it needs and the rest of the page can render.

Canonical *_tool.py exports: TOOL_NAME, Request, Response, run().
"""
from __future__ import annotations

import asyncio
from typing import Optional

from pydantic import BaseModel, Field

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.authentication.agent_deps import AgentDeps
from chathealthy_lib.authentication.chathealthy_tool import ChatHealthyTool
from chathealthy_lib.capability_contract import CapabilityContract

log = ChatHealthyLoggingService()

# The located parts the searches consume. position is not extracted from
# prose; it is set by the map when there is one, which there is not yet.
_GEO_PARTS = ("state", "city", "zip", "county")


class Request(BaseModel):
    """Which page's geography to resolve, and the text to resolve it from.

    page is required because geography is page-scoped: individualProvider
    and facility hold their own, and a tool that wrote another page's
    parameter would defeat the namespaces the pages exist for.
    """
    model_config = {"extra": "ignore"}
    page: str = Field(
        description="The page this resolves geography for — individualProvider "
                    "or facility. The tool writes onto this page and no other.")
    utterance: str = Field(
        description="The free text the location is extracted from. Carries no "
                    "specialty and finds no providers.")


class Response(BaseModel):
    page: str = ""
    state: Optional[str] = None
    county: Optional[str] = None
    city: Optional[str] = None
    zip: Optional[str] = None


class GeoTool(ChatHealthyTool, CapabilityContract):
    """Free text -> structured US location, written onto the named page's
    geography. The parallel of SpecialtyFilter for the geography axis."""
    TOOL_NAME = "geo"
    CAPABILITY = "geo"

    TOOL_DESCRIPTION = (
        "Extracts a US location from an utterance and writes it onto the "
        "page's own (page-scoped) geography parameter.")
    # Dispatched by the UtteranceManager as an extraction step, not by a gate
    # op and never by a free-text utterance of its own.
    SUBSCRIPTIONS: list[str] = []
    MAY_CALL: list[str] = ["user_parameters"]
    NOT_UTTERANCE_ROUTABLE = True
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        page = (request.page or "").strip()
        if not page:
            raise ChatHealthyException(
                mode="value_error",
                component="geo_tool",
                message="Geo requires Request.page; geography is page-scoped, "
                        "so the tool must be told which page it services.")

        text = (request.utterance or "").strip()
        if not text:
            return self.Response(page=page)

        from chathealthy_lib.geo_extractor import extract_location
        located = await asyncio.to_thread(extract_location, text)
        stated = located.model_dump(exclude_none=True)
        parts = {k: v for k, v in stated.items() if k in _GEO_PARTS and v}

        if parts:
            from UserParameters import user_parameters_tool
            changes = [
                user_parameters_tool.Change(page=page, name=name, value=value)
                for name, value in parts.items()
            ]
            # route="utterance_manager": geography resolution is part of the
            # utterance manager's turn, and only the UM and the gateway may
            # write a page a tool does not itself belong to. origin is
            # non_deterministic because the place was inferred from prose.
            await user_parameters_tool.TOOL.run(
                deps,
                user_parameters_tool.Request(
                    verb="set",
                    changes=changes,
                    route="utterance_manager",
                    origin="non_deterministic",
                ),
            )

        return self.Response(page=page, **parts)


TOOL = GeoTool()
