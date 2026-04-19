# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# evaluate_providers_graph.py — LangGraph orchestrator for
# POST /evaluate/providers.
#
# v4-043: wraps the FindCare → EvaluateCare provider-handoff handler.
# Two logical steps modeled as graph nodes:
#   1. verify_token  — verify the signed session token from FindCare
#   2. record        — store providers + question in the in-memory cache
#                      and shape the response payload
#
# No behavior change — same response shape and side effects as the
# original handler.

import logging
import os
import sys

from typing_extensions import TypedDict  # langgraph schema introspection requires this on Py<3.12

from langgraph.graph import StateGraph, START, END


_log = logging.getLogger("evaluate_care")


class EvaluateProvidersState(TypedDict, total=False):
    providers: list
    chat_history: list
    session_token: dict
    question_summary: str
    token_valid: bool
    token_origin: str
    evaluated_providers: list
    response: dict


def _verify_token_node(state):
    """Verify session token from FindCare (mirrors original handler)."""
    token = state.get("session_token")
    token_valid = False
    token_origin = "unknown"
    if token:
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "Shared"))
            os.environ.setdefault(
                "CERTS_DIR",
                os.path.join(os.path.dirname(__file__), "..", "..", "Shared", "ops", "certs"),
            )
            from session_token import verify_session_token
            token_valid = verify_session_token(token, "FindCare")
            token_origin = token.get("origin", "unknown")
            _log.info(
                "Session token: origin=%s valid=%s token=%s",
                token_origin, token_valid, token.get("token", "?")[:20],
            )
        except Exception as e:
            _log.warning("Session token verification failed: %s", e)
    return {"token_valid": token_valid, "token_origin": token_origin}


def _record_node(state, *, services):
    """Capture the provider list, update the shared last-evaluation
    store, and shape the JSON response."""
    last_evaluation = services["last_evaluation"]

    results = []
    for p in state.get("providers", []):
        results.append({
            "name": p.get("name", "Unknown"),
            "specialty": p.get("specialty", p.get("primary_specialty", "Unknown")),
            "npi": p.get("npi", "Unknown"),
        })
    last_evaluation["providers"] = results
    last_evaluation["question"] = state.get("question_summary") or "Provider evaluation"

    token = state.get("session_token")
    token_valid = state.get("token_valid", False)
    _log.info(
        "Received %d providers from FindCare (token_valid=%s): %s",
        len(results), token_valid, last_evaluation["question"],
    )

    response = {
        "status": "received",
        "evaluated_providers": results,
        "question_summary": last_evaluation["question"],
        "session_token": {
            "token_received": token.get("token", "") if token else "",
            "signature_received": (
                (token.get("signature", "") or "")[:40] + "..."
                if token and token.get("signature") else "none"
            ),
            "origin": token.get("origin", "") if token else "",
            "verified": token_valid,
        },
    }
    return {"evaluated_providers": results, "response": response}


def build_evaluate_providers_graph(services):
    """Compile the /evaluate/providers StateGraph.

    `services` is a dict carrying the shared in-memory `last_evaluation`
    store under the key 'last_evaluation'."""
    g = StateGraph(EvaluateProvidersState)
    g.add_node("verify_token", _verify_token_node)
    g.add_node("record", lambda s: _record_node(s, services=services))
    g.add_edge(START, "verify_token")
    g.add_edge("verify_token", "record")
    g.add_edge("record", END)
    return g.compile()
