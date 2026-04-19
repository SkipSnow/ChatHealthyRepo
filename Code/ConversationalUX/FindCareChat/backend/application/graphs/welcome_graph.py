# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# welcome_graph.py — LangGraph StateGraph for the GET /welcome handler.
#
# Returns the static welcome message produced by PromptSystemMaker. Wrapped
# in a graph solely to satisfy engineering rule v4-043 (every business-logic
# route handler must invoke through a graph orchestrator).

from typing_extensions import TypedDict  # langgraph schema introspection requires this on Py<3.12

from langgraph.graph import StateGraph, START, END


class WelcomeState(TypedDict, total=False):
    response: dict


def build_welcome_graph(welcome_message: str):
    """Build and compile the /welcome graph.

    Args:
        welcome_message: the WELCOME_MESSAGE constant from main.py.
    """

    def welcome_node(state: WelcomeState) -> dict:
        return {"response": {"message": welcome_message}}

    g = StateGraph(WelcomeState)
    g.add_node("welcome", welcome_node)
    g.add_edge(START, "welcome")
    g.add_edge("welcome", END)
    return g.compile()
