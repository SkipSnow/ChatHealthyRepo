# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# score_provider_graph.py — LangGraph orchestrator for POST /score/provider.
#
# v4-043: wraps the existing ScoringEngine.score_provider() call as a
# graph node. No business logic changes — same inputs, same outputs.

from typing_extensions import TypedDict  # langgraph schema introspection requires this on Py<3.12

from langgraph.graph import StateGraph, START, END


class ScoreProviderState(TypedDict, total=False):
    request: dict
    response: dict


def _coerce_measure_inputs(raw_measures):
    """Build MeasureInput list from request dicts.

    Auto-routes numeric values to .value (and bool to 1.0/0.0) and
    non-numeric values to .raw_value — identical to the pre-graph
    handler body."""
    from evaluate_care.models import MeasureInput

    measure_inputs = []
    for m in raw_measures:
        val = m.get("value")
        if isinstance(val, (int, float, bool)):
            measure_inputs.append(MeasureInput(
                name=m["name"],
                value=float(val) if not isinstance(val, bool) else (1.0 if val else 0.0),
                raw_value=val,
            ))
        else:
            measure_inputs.append(MeasureInput(name=m["name"], raw_value=val))
    return measure_inputs


def _score_provider_node(state, *, services):
    req = state["request"]
    measure_inputs = _coerce_measure_inputs(req["measures"])
    result = services["engine"].score_provider(req["provider_id"], measure_inputs)
    return {"response": result.model_dump()}


def build_score_provider_graph(services):
    """Compile the /score/provider StateGraph.

    `services` is a dict carrying the live ScoringEngine instance under
    the key 'engine' — passed in from app.py at startup."""
    g = StateGraph(ScoreProviderState)
    g.add_node("score_provider", lambda s: _score_provider_node(s, services=services))
    g.add_edge(START, "score_provider")
    g.add_edge("score_provider", END)
    return g.compile()
