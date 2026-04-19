# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# qa_report_submit_graph.py — LangGraph StateGraph for POST /qa-report.
#
# Persists QA-report edits posted from the rendered form back to MongoDB.
# Wrapped in a graph solely to satisfy engineering rule v4-043. The handler
# is async because it awaits request.form(); the graph itself is sync — the
# pre-awaited form dict is passed in as state.

import logging
from typing import Any, Callable, Dict
from typing_extensions import TypedDict  # langgraph schema introspection requires this on Py<3.12

from langgraph.graph import StateGraph, START, END
from starlette.responses import RedirectResponse


_log = logging.getLogger("findcare.graph.qa_report_submit")


class QAReportSubmitState(TypedDict, total=False):
    # ── inputs ──
    env_prefix: str
    form: Dict[str, str]   # already-awaited request.form() entries
    # ── outputs ──
    response: Any          # dict {"error": ...} or RedirectResponse


def build_qa_report_submit_graph(
    get_qa_report_fn: Callable[[], dict],
    get_db_fn: Callable[[], Any],
):
    """Build and compile the POST /qa-report graph.

    Args:
        get_qa_report_fn: zero-arg callable returning the QA report dict.
        get_db_fn: zero-arg callable returning the MongoDB connection (or None).
    """

    def submit_node(state: QAReportSubmitState) -> dict:
        """Apply form edits to the report doc and persist — straight port."""
        env_prefix = state.get("env_prefix", "")
        if env_prefix == "prod":
            return {"response": {"error": "QA report not available in production"}}
        report = get_qa_report_fn()
        if not report:
            return {"response": {"error": "QA report not found"}}
        form = state.get("form", {}) or {}
        updated = 0
        for feat in report.get("features", []):
            for tc in feat.get("test_cases", []):
                tc_id = tc.get("tc", "")
                if tc_id in form and form[tc_id]:
                    tc["status"] = form[tc_id]
                    updated += 1
        # Recompute summary
        all_tc = [tc for f in report["features"] for tc in f.get("test_cases", [])]
        report["summary"]["pass"] = sum(1 for tc in all_tc if tc.get("status") == "PASS")
        report["summary"]["in_progress"] = sum(1 for tc in all_tc if tc.get("status") == "IN_PROGRESS")
        report["summary"]["not_started"] = sum(1 for tc in all_tc if tc.get("status") in ("NOT_STARTED", ""))
        # Write to MongoDB
        db = get_db_fn()
        if db:
            db[f"{env_prefix}_System"]["bugs"].replace_one(
                {"_record_id": "qa_report_v014_sit"}, report, upsert=True)
        _log.info("QA report saved to MongoDB: %d test cases updated", updated)
        return {"response": RedirectResponse(url="/qa-report", status_code=303)}

    g = StateGraph(QAReportSubmitState)
    g.add_node("submit", submit_node)
    g.add_edge(START, "submit")
    g.add_edge("submit", END)
    return g.compile()
