# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# explain_graph.py — LangGraph orchestrator for POST /explain.
#
# v4-043: wraps the existing explain_score() call as a graph node.
# Behavior preserved exactly — the handler historically passed the raw
# request dict straight into explain_score(), and this graph does the
# same so existing callers see the same response.

from typing_extensions import TypedDict  # langgraph schema introspection requires this on Py<3.12

from langgraph.graph import StateGraph, START, END


class ExplainState(TypedDict, total=False):
    score_output: dict
    response: object


def _explain_node(state, *, services):
    explain_fn = services["explain_fn"]
    return {"response": explain_fn(state["score_output"])}


def build_explain_graph(services):
    """Compile the /explain StateGraph.

    `services` is a dict carrying the explain_score callable under
    the key 'explain_fn'."""
    g = StateGraph(ExplainState)
    g.add_node("explain", lambda s: _explain_node(s, services=services))
    g.add_edge(START, "explain")
    g.add_edge("explain", END)
    return g.compile()
