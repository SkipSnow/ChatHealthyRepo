# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""UtteranceManager — SharedServices orchestrator for one user utterance.

Dispatched by UniversalNavigationTool when ``op == "utterance"``. Owns
the parallel fan-out across the per-concern tools the application needs
to run on every utterance. Today's tools:

  1. SpecialtyFilter (HTTPS hop into FindCare; runs ``find_specialists``)
  2. GeoExtractor    (in-process; runs the shared-lib LLM call)

Tomorrow the same fan-out will add Safety, Unknown-Question, Clinical
Trials, Lead Capture, etc. — each as its own independent tool, never
bundled into another tool's LLM call.

Streaming contract: each tool returns one JSON to this orchestrator.
The orchestrator emits a stream event to the FE as soon as a tool's
JSON arrives that the FE can render — today that is SpecialtyFilter's
result (the SpecialtyFilter widget). Geo is internal state; no widget,
no stream event. Once both LLM tools have returned and the sufficiency
gate passes, the deterministic ProviderSearch delegate runs and its
result is streamed to the FE. The final ``kind: "final"`` event closes
the NDJSON stream.

Sufficiency gate:
  * NUCC codes non-empty (SpecialtyFilter returned at least one pick).
  * Usable location: zip alone, OR state alone, OR state+city, OR
    state+county. City or county without state is not sufficient.

If either side fails the gate the orchestrator emits a final error
event and returns without running the provider query.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from authentication.agent_deps import AgentDeps, log_utterance
from authentication import provider_search_and_selection_tool
from SpecialtyFilter import specialty_filter_tool
from UtteranceManager import geo_extractor_tool

_log = logging.getLogger("shared_services.utterance_manager")


def _usable_location(geo: geo_extractor_tool.Response) -> bool:
    """The four sufficiency cases: zip alone, state alone, state+city,
    state+county. State alone covers state+anything; zip alone is its
    own case. City or county without state is not sufficient.
    """
    return bool(geo.zip or geo.state)


async def run(deps: AgentDeps, text: str) -> dict[str, Any]:
    """Run one utterance through the parallel fan-out + sufficiency gate
    + provider query. Returns the result dict the navigation orchestrator
    wraps in its Response envelope.
    """
    # Person stream — what the user typed.
    log_utterance(deps.user_object, text)

    # Parallel: SpecialtyFilter (HTTPS to FindCare) + GeoExtractor (lib).
    # Each tool returns one JSON; this orchestrator decides what to stream.
    fs_task = specialty_filter_tool.TOOL.run_and_log(
        deps, specialty_filter_tool.Request()
    )
    geo_task = geo_extractor_tool.TOOL.run_and_log(
        deps, geo_extractor_tool.Request()
    )
    fs, geo = await asyncio.gather(fs_task, geo_task)

    # SpecialtyFilter result is what the FE renders as the specialty
    # widget; emit it to the stream as soon as we have it.
    deps.stream({"kind": "specialties", "data": fs.model_dump(exclude_none=True)})

    # Sufficiency gate 1: did SpecialtyFilter produce picks?
    if fs.error or not fs.specialties:
        deps.stream({
            "kind": "final",
            "data": {"ok": False, "error": fs.error or "no_specialties"},
        })
        return {
            "ok": False,
            "classify": fs.model_dump(exclude_none=True),
            "geo": geo.model_dump(exclude_none=True),
        }

    # Sufficiency gate 2: is the location usable?
    if geo.error or not _usable_location(geo):
        deps.stream({
            "kind": "final",
            "data": {"ok": False, "error": geo.error or "insufficient_location"},
        })
        return {
            "ok": False,
            "classify": fs.model_dump(exclude_none=True),
            "geo": geo.model_dump(exclude_none=True),
        }

    # Both LLM tools satisfied — run the deterministic provider query.
    codes = [s.code for s in fs.specialties]
    ps_req = provider_search_and_selection_tool.Request(
        specialty_codes=codes,
        state=geo.state,
        city=geo.city,
        county=geo.county,
        zip=geo.zip,
        limit=25,
    )
    ps = await provider_search_and_selection_tool.TOOL.run_and_log(deps, ps_req)

    deps.stream({"kind": "final", "data": {"ok": True}})
    return {
        "ok": True,
        "classify": fs.model_dump(exclude_none=True),
        "geo": geo.model_dump(exclude_none=True),
        "search": ps.model_dump(exclude_none=True),
    }
