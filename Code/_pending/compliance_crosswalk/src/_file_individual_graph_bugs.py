# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# One-shot apply: file one tracking bug per non-graph FastAPI handler
# (per Human directive 2026-04-19 — "one bug for every LangGraph
# container that has no content / has an error"). Replaces the prior
# consolidated BUG-ARCH-GRAPH-EXEMPT-001 (kept as the umbrella) with
# individual per-container bugs that each link back to it.

from __future__ import annotations

import json
from pathlib import Path
from datetime import date

REPO = Path(r"c:/chatHealthy/findCare")
BUGS = REPO / "brain/machine_artifacts/content/bugs.json"
TODAY = date.today().isoformat()

# Authoritative list of every FastAPI handler that does NOT yet route
# through a real LangGraph StateGraph. Source: pre_deploy_rule_check.py
# v4-043 graph_entry_check executor scan (pre-exemption). 30 entries.
HANDLERS = [
    # main.py — 9 mechanical (currently exempted) + 5 business-logic
    ("Code/ConversationalUX/FindCareChat/backend/main.py", 595, "health", "mechanical"),
    ("Code/ConversationalUX/FindCareChat/backend/main.py", 625, "get_session", "mechanical"),
    ("Code/ConversationalUX/FindCareChat/backend/main.py", 656, "evaluate_splash", "mechanical"),
    ("Code/ConversationalUX/FindCareChat/backend/main.py", 679, "transfer_to_findcare", "mechanical"),
    ("Code/ConversationalUX/FindCareChat/backend/main.py", 703, "evaluate_proxy", "mechanical"),
    ("Code/ConversationalUX/FindCareChat/backend/main.py", 742, "shared_splash", "mechanical"),
    ("Code/ConversationalUX/FindCareChat/backend/main.py", 765, "shared_verify_token", "mechanical"),
    ("Code/ConversationalUX/FindCareChat/backend/main.py", 789, "evaluate_verify_token", "mechanical"),
    ("Code/ConversationalUX/FindCareChat/backend/main.py", 964, "serve_index", "mechanical"),
    ("Code/ConversationalUX/FindCareChat/backend/main.py", 370, "search", "business-logic"),
    ("Code/ConversationalUX/FindCareChat/backend/main.py", 383, "classify", "business-logic"),
    ("Code/ConversationalUX/FindCareChat/backend/main.py", 482, "qa_report", "business-logic"),
    ("Code/ConversationalUX/FindCareChat/backend/main.py", 539, "qa_report_submit", "business-logic"),
    ("Code/ConversationalUX/FindCareChat/backend/main.py", 569, "welcome", "business-logic"),
    # evaluate_care/app.py — 6 mechanical + 5 business-logic
    ("Code/evaluate_care/app.py", 94, "debug_verify_live", "mechanical"),
    ("Code/evaluate_care/app.py", 127, "debug_bootstrap", "mechanical"),
    ("Code/evaluate_care/app.py", 172, "health", "mechanical"),
    ("Code/evaluate_care/app.py", 192, "splash", "mechanical"),
    ("Code/evaluate_care/app.py", 200, "transfer_to_findcare", "mechanical"),
    ("Code/evaluate_care/app.py", 214, "verify_token", "mechanical"),
    ("Code/evaluate_care/app.py", 251, "score_provider", "business-logic"),
    ("Code/evaluate_care/app.py", 271, "score_trial", "business-logic"),
    ("Code/evaluate_care/app.py", 289, "explain", "business-logic"),
    ("Code/evaluate_care/app.py", 304, "evaluate_providers", "business-logic"),
    ("Code/evaluate_care/app.py", 346, "evaluate_view", "business-logic"),
    # shared_services/app.py — 5 mechanical
    ("Code/shared_services/app.py", 108, "health", "mechanical"),
    ("Code/shared_services/app.py", 129, "splash", "mechanical"),
    ("Code/shared_services/app.py", 137, "transfer_to_findcare", "mechanical"),
    ("Code/shared_services/app.py", 150, "verify_token", "mechanical"),
    ("Code/shared_services/app.py", 176, "get_secret", "mechanical"),
]

# Severity scheme (per v4-041 severity-by-environment intent baked into description)
SEVERITY = {
    "business-logic": ("high", "dev=high, qa=critical, prod=show_stopper"),
    "mechanical":     ("low",  "dev=low, qa=medium, prod=high"),
}


def _bugid(file_path: str, function: str) -> str:
    """Stable bug id: BUG-LANGCHAIN-CONTAINER-<short_path>-<function>."""
    short = file_path.split("/")[-1].replace(".py", "").upper().replace("_", "-")
    return f"BUG-LANGCHAIN-CONTAINER-{short}-{function.upper().replace('_', '-')}"


def main():
    with open(BUGS, encoding="utf-8") as f:
        bg = json.load(f)
    bugs = bg.get("bug", [])
    existing = {b.get("bugid") for b in bugs}

    added = 0
    for file_path, line, function, kind in HANDLERS:
        bid = _bugid(file_path, function)
        if bid in existing:
            continue
        sev, sev_intent = SEVERITY[kind]
        bugs.append({
            "bugid": bid,
            "title": (f"Empty LangChain container: {function}() in "
                      f"{file_path.split('/')[-1]}:{line} bypasses graph "
                      f"orchestrator (v4-043) — {kind}"),
            "description": (
                f"FastAPI route handler '{function}' at {file_path}:{line} "
                f"does not route business logic through a LangGraph "
                f"StateGraph — it is an empty LangChain container with no "
                f"graph content. Detected by v4-043 graph_entry_check "
                f"executor in preCommitScan.py / pre_deploy_rule_check.py. "
                f"Classification: {kind}. Per v4-041 "
                f"severity_by_environment intent: {sev_intent}. Closes "
                f"only when the handler invokes a real graph "
                f"(graph.invoke()) or is replaced by a graph-routed "
                f"equivalent. Tracks one container; the umbrella "
                f"refactor work plan lives in BUG-ARCH-GRAPH-001 "
                f"(business-logic) / BUG-ARCH-GRAPH-EXEMPT-001 "
                f"(mechanical exemptions, transitional)."
            ),
            "environments": ["dev"],
            "status": "deferred",
            "severity": sev,
            "req_id": "EPIC-008-F-010-S-001-REQ-T-001",
            "story_id": "ARCH-GRAPH-ORCHESTRATION-001",
            "pytest_id": "TBD-graph-routed-handler-test",
            "pytest_success_criteria": [
                f"v4-043 graph_entry_check reports zero violations for "
                f"{function} in {file_path}",
                f"Studio renders the graph that hosts {function} as a node",
                f"Existing functional behavior of {function} is unchanged",
            ],
            "discovery_date": TODAY,
            "due_date": "2026-05-15",
            "fix": "",
            "reproduction": (
                f"Run: python Code/Shared/ops/tools/pre_deploy_rule_check.py "
                f"findcare 2>&1 | grep '{function}'. Expected after fix: "
                f"zero v4-043 violations for this handler."
            ),
            "orphan": False,
            "next_action": [
                f"Wrap {function} in a real LangGraph StateGraph; route the "
                f"handler body through graph.invoke()."
            ],
        })
        added += 1
    bg["bug"] = bugs
    with open(BUGS, "w", encoding="utf-8") as f:
        json.dump(bg, f, indent=2)
    print(f"Per-container bugs filed: {added} (total {len(HANDLERS)} handlers; "
          f"{len(HANDLERS) - added} already existed)")


if __name__ == "__main__":
    main()
