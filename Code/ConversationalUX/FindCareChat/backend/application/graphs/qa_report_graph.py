# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# qa_report_graph.py — LangGraph StateGraph for the GET /qa-report handler.
#
# Renders the editable QA-report HTML from MongoDB. Pure presentation —
# no LLM, no provider services. Wrapped in a graph solely to satisfy
# engineering rule v4-043 (every business-logic route handler must invoke
# through a graph orchestrator).

from typing import Any, Callable
from typing_extensions import TypedDict  # langgraph schema introspection requires this on Py<3.12

from langgraph.graph import StateGraph, START, END
from starlette.responses import HTMLResponse


class QAReportState(TypedDict, total=False):
    # ── inputs ──
    env_prefix: str
    # ── outputs ──
    response: Any        # dict {"error": ...} or HTMLResponse


def build_qa_report_graph(get_qa_report_fn: Callable[[], dict]):
    """Build and compile the GET /qa-report graph.

    Args:
        get_qa_report_fn: zero-arg callable that returns the QA report dict
                         from MongoDB (with file bootstrap fallback).
    """

    def render_node(state: QAReportState) -> dict:
        """Render the QA report HTML — straight port of the original handler."""
        env_prefix = state.get("env_prefix", "")
        if env_prefix == "prod":
            return {"response": {"error": "QA report not available in production"}}
        report = get_qa_report_fn()
        if not report:
            return {"response": {"error": "QA report not found"}}
        features = report.get("features", [])
        # Build HTML with editable dropdowns (DEVOPS-QA-005)
        options = "".join(f'<option value="{s}">{s}</option>' for s in
                          ["", "PASS", "FAIL", "DEFERRED", "NOT_STARTED",
                           "IN_PROGRESS", "TO_TEST", "UNTESTED", "RELEASE_BLOCKER"])
        rows = ""
        for feat in features:
            status = feat.get("status", "NOT_STARTED")
            color = {"PASS": "#059669", "IN_PROGRESS": "#2563eb", "NOT_STARTED": "#9ca3af",
                     "UNTESTED": "#d97706", "RELEASE_BLOCKER": "#dc2626", "TO_TEST": "#7c3aed"}.get(status, "#6b7280")
            tc_count = len(feat.get("test_cases", []))
            tc_pass = sum(1 for tc in feat.get("test_cases", []) if tc.get("status") == "PASS")
            rows += f'<tr style="background:#f9fafb"><td>{feat.get("id","")}</td><td><b>{feat.get("feature_id","")}</b></td>'
            rows += f'<td><b>{feat.get("name","")}</b></td><td>{feat.get("epic","")}</td>'
            rows += f'<td style="color:{color};font-weight:600">{status}</td>'
            rows += f'<td>{tc_pass}/{tc_count}</td></tr>\n'
            for tc in feat.get("test_cases", []):
                tc_id = tc.get("tc", "")
                tc_status = tc.get("status", "")
                sel_options = options.replace(f'value="{tc_status}"', f'value="{tc_status}" selected')
                rows += f'<tr style="font-size:12px"><td></td><td></td>'
                rows += f'<td style="padding-left:24px">{tc_id}: {tc.get("test","")}</td>'
                rows += f'<td></td><td><select name="{tc_id}" style="font-size:11px;padding:2px">{sel_options}</select></td><td></td></tr>\n'
        summary = report.get("summary", {})
        html = f"""<!DOCTYPE html><html><head><title>QA Report — {report.get('version','')}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1000px;margin:40px auto;padding:0 20px}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #e5e7eb;padding:8px 12px;text-align:left}}
th{{background:#f3f4f6;font-size:13px}}tr:hover{{background:#f9fafb}}
h1{{font-size:24px}}h2{{font-size:16px;color:#6b7280}}
select{{border:1px solid #d1d5db;border-radius:4px}}
.submit-btn{{background:linear-gradient(180deg,#0b9a94,#0b7a75);color:#fff;border:none;padding:10px 24px;
border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;margin-top:16px}}
.submit-btn:hover{{opacity:0.9}}</style></head><body>
<form method="POST" action="/qa-report">
<h1>QA Report — {report.get('version','')} Dev→SIT</h1>
<h2>{report.get('scope','')}</h2>
<p>Date: {report.get('date','')} | Target: {report.get('target','')} | Status: {report.get('status','')}</p>
<p><b>Features:</b> {summary.get('total_features',0)} | <b>Test Cases:</b> {summary.get('total_test_cases',0)} |
<b style="color:#059669">Pass:</b> {summary.get('pass',0)} |
<b style="color:#2563eb">In Progress:</b> {summary.get('in_progress',0)} |
<b style="color:#9ca3af">Not Started:</b> {summary.get('not_started',0)}</p>
<table><tr><th>#</th><th>Feature ID</th><th>Name</th><th>Epic</th><th>Status</th><th>Tests</th></tr>
{rows}</table>
<button type="submit" class="submit-btn">Save QA Report</button>
</form>
<p style="font-size:11px;color:#9ca3af;margin-top:24px">&copy; 2026 Skip Snow. All rights reserved.</p>
</body></html>"""
        return {"response": HTMLResponse(content=html)}

    g = StateGraph(QAReportState)
    g.add_node("render", render_node)
    g.add_edge(START, "render")
    g.add_edge("render", END)
    return g.compile()
