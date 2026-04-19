# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# evaluate_view_graph.py — LangGraph orchestrator for GET /evaluate/view.
#
# v4-043: wraps the HTML page renderer for the FindCare → EvaluateCare
# handoff result. The handler has no business inputs (just reads the
# shared last-evaluation store), so the graph is a single render node.
# HTML output is byte-identical to the pre-graph handler.

from typing_extensions import TypedDict  # langgraph schema introspection requires this on Py<3.12

from langgraph.graph import StateGraph, START, END


class EvaluateViewState(TypedDict, total=False):
    html: str


def _render_view_node(state, *, services):
    last_evaluation = services["last_evaluation"]
    providers = last_evaluation.get("providers", [])
    question = last_evaluation.get("question", "No evaluation received yet")

    rows = ""
    for i, p in enumerate(providers):
        rows += (
            f"<tr><td>{i+1}</td><td>{p['name']}</td>"
            f"<td>{p['specialty']}</td><td>{p['npi']}</td></tr>"
        )
    if not rows:
        rows = (
            "<tr><td colspan='4' style='text-align:center;color:#999;'>"
            "No providers received yet. Click 'Evaluate These Providers' in FindCare."
            "</td></tr>"
        )
    html = f"""<!DOCTYPE html>
<html><head><title>EvaluateCare — Provider Evaluation</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 40px; background: #f8fffe; }}
h1 {{ color: #0b7a75; }}
h2 {{ color: #374151; font-size: 16px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
th {{ background: #0b7a75; color: white; padding: 10px; text-align: left; }}
td {{ padding: 8px 10px; border-bottom: 1px solid #e5e7eb; }}
tr:hover {{ background: #f0fffe; }}
.badge {{ background: #d1fae5; color: #065f46; padding: 2px 8px; border-radius: 4px; font-size: 12px; }}
.header {{ display: flex; justify-content: space-between; align-items: center; }}
</style></head><body>
<div class="header">
    <h1>EvaluateCare — Provider Evaluation</h1>
    <span class="badge">Service: localhost:8001 (separate from FindCare)</span>
</div>
<h2>Query: {question}</h2>
<p>{len(providers)} providers received from FindCare via handoff</p>
<table>
<tr><th>#</th><th>Provider</th><th>Specialty</th><th>NPI</th></tr>
{rows}
</table>
<p style="color:#999;font-size:12px;margin-top:24px;">
    This page proves the FindCare → EvaluateCare handoff. Providers were sent from FindCare (:8000) to EvaluateCare (:8001) as a separate service.
    In production, this communication uses mTLS with x509 certificates.
</p>
</body></html>"""
    return {"html": html}


def build_evaluate_view_graph(services):
    """Compile the /evaluate/view StateGraph.

    `services` is a dict carrying the shared in-memory `last_evaluation`
    store under the key 'last_evaluation'."""
    g = StateGraph(EvaluateViewState)
    g.add_node("render", lambda s: _render_view_node(s, services=services))
    g.add_edge(START, "render")
    g.add_edge("render", END)
    return g.compile()
