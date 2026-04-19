# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# search_graph.py — LangGraph StateGraph for the /search FastAPI handler.
#
# Pattern: thin wrapper around FindCareService.search_providers. Same input,
# same output as the original handler body. Exists so main.py can satisfy
# engineering rule v4-043 (every business-logic route handler must invoke
# through a graph).

from typing import Any
from typing_extensions import TypedDict  # langgraph schema introspection requires this on Py<3.12

from langgraph.graph import StateGraph, START, END


class SearchState(TypedDict, total=False):
    # ── inputs ──
    params: dict          # SearchRequest.model_dump(exclude_none=True)
    # ── outputs ──
    response: Any         # whatever FindCareService.search_providers returns


def _search_node(state: SearchState, *, find_care) -> dict:
    """Direct provider search — no LLM. Mirrors the original /search body."""
    params = state.get("params", {}) or {}
    result = find_care.search_providers(**params)
    return {"response": result}


def build_search_graph(find_care):
    """Build and compile the /search graph.

    Args:
        find_care: FindCareService instance (the existing _find_care).
    """
    g = StateGraph(SearchState)
    g.add_node("search", lambda s: _search_node(s, find_care=find_care))
    g.add_edge(START, "search")
    g.add_edge("search", END)
    return g.compile()
