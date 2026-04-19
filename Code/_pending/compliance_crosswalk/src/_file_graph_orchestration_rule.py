# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# One-shot apply: governance for the graph-orchestration architecture rule
# Human directed 2026-04-19.
#
# What:
#   1. Engineering rule v4-043 — every business-logic entry point routes
#      through a graph orchestrator; enforced at check-in.
#   2. Story + B+T requirement pair under EPIC-008/ARCH-GRAPH-ORCHESTRATION
#      (new feature) capturing the architectural intent + the scanner
#      that must enforce it.
#   3. BUG-ARCH-GRAPH-001 — /classify endpoint currently bypasses the
#      LangGraph orchestrator (refactor pending).
#   4. BUG-ARCH-GRAPH-002 — pre-commit scanner does not yet detect
#      non-graph entry points (executor not built).

from __future__ import annotations

import json
from pathlib import Path
from datetime import date

REPO = Path(r"c:/chatHealthy/findCare")
BACKLOG = REPO / "brain/machine_artifacts/content/agile_backlog.json"
BUGS = REPO / "brain/machine_artifacts/content/bugs.json"
RULES = REPO / "brain/machine_artifacts/content/engineering_rules.json"
TODAY = date.today().isoformat()


def _orphan_pytest():
    return {"pytest": [{
        "pytest_id": "TBD-graph-orchestration-scanner-not-built",
        "pytest_type": "ORPHAN",
        "orphan": True,
    }]}


# ── 1. Engineering rule v4-043 ──


def add_rule():
    with open(RULES, encoding="utf-8") as f:
        er = json.load(f)
    rules = er.get("rules", [])
    if any(r.get("id") == "v4-043" for r in rules):
        return False
    rules.append({
        "id": "v4-043",
        "title": "All business-logic entry points route through a graph orchestrator",
        "rule": (
            "Every business-logic entry point in the codebase MUST invoke "
            "through a graph orchestrator (today: LangGraph StateGraph) "
            "rather than directly calling business services from the "
            "endpoint handler. Business-logic entry points include: "
            "FastAPI/Flask/etc. route handlers, scheduled triggers, CLI "
            "command handlers, message/queue consumers, and any other "
            "operationally-invoked surface. Pure utility libraries (data "
            "structures, serialization, string formatting, math) are "
            "exempt. The rationale is single-source-of-truth orchestration: "
            "shared concerns (safety, auth, audit, classify) live as graph "
            "nodes invoked once per request, so a change to any shared "
            "concern propagates to every entry point automatically. "
            "Parallel paths that call the same shared service directly "
            "from a non-graph handler (e.g., the current /classify "
            "endpoint that bypasses _chat_graph and calls "
            "safety_service.is_emergency() directly) are violations of "
            "this rule. Pre-commit scanner MUST detect: any "
            "FastAPI/Flask route handler function body that does not "
            "include a graph.invoke() (or equivalent StateGraph entry) "
            "call as its primary action — the handler is permitted to "
            "do request parsing, response shaping, and exception "
            "translation, but the business work itself MUST flow "
            "through the graph."
        ),
        "type": "standard",
        "date": TODAY,
        "source": ("human directive 2026-04-19 - graph-orchestration as "
                   "architecture rule, enforced at check-in"),
        "enforcement": [{
            "type": "graph_entry_check",
            "_executor_status": "NOT YET IMPLEMENTED — see BUG-ARCH-GRAPH-002",
            "scan_dirs": [
                "Code/ConversationalUX/FindCareChat/backend",
                "Code/evaluate_care",
                "Code/shared_services",
            ],
            "_note": ("Detects FastAPI/Flask route handlers (functions "
                      "decorated with @app.post/@app.get/@router.post/etc.) "
                      "whose body does not contain a call matching "
                      "<state_graph>.invoke(...). Permitted exceptions: "
                      "handlers explicitly marked with a # graph-exempt "
                      "comment AND a paired risk_acceptance entry."),
        }],
    })
    er["rules"] = rules
    with open(RULES, "w", encoding="utf-8") as f:
        json.dump(er, f, indent=2)
    return True


# ── 2. New feature ARCH-GRAPH-ORCHESTRATION + story + B/T reqs ──


def add_backlog():
    with open(BACKLOG, encoding="utf-8") as f:
        bl = json.load(f)
    epic_008 = next(e for e in bl["epics"]["epic"]
                    if e.get("epic_id") == "EPIC-008" and e.get("name") == "Architecture")
    feats = epic_008.get("features", {}).get("feature", []) if isinstance(
        epic_008.get("features"), dict) else epic_008.get("features", [])
    if any((f.get("feature_id") or f.get("id")) == "ARCH-GRAPH-ORCHESTRATION"
           for f in feats):
        return False

    feats.append({
        "feature_id": "ARCH-GRAPH-ORCHESTRATION",
        "name": "Graph-orchestrated business-logic entry points",
        "status": "in_progress",
        "description": (
            "Every business-logic entry point routes through a graph "
            "orchestrator (today LangGraph) so shared concerns - safety, "
            "auth, audit, classification - live in one place and propagate "
            "automatically to every entry point. Encoded as engineering "
            "rule v4-043. Currently violated by /classify endpoint."
        ),
        "orphan": False,
        "approval": "approved",
        "stories": {"story": [{
            "story_id": "ARCH-GRAPH-ORCHESTRATION-001",
            "name": "Codify graph-orchestration rule and enforce at check-in",
            "title": "Codify graph-orchestration rule and enforce at check-in",
            "description": (
                "Engineering rule v4-043 + a pre-commit scanner that "
                "rejects any new business-logic entry point that does "
                "not invoke through the graph. Existing violators "
                "(/classify) get tracking bugs and refactor stories."
            ),
            "size": "M",
            "status": "in_progress",
            "orphan": False,
            "approval": "approved",
            "requirements": {"requirement": [
                {
                    "req_id": "ARCH-GRAPH-ORCHESTRATION-001-REQ-B001",
                    "requirement": (
                        "Every operational entry point in ChatHealthy.ai "
                        "Python services MUST invoke business logic "
                        "through a single graph orchestrator. Shared "
                        "concerns - safety, auth, audit, classification "
                        "- are graph nodes; updating a shared concern "
                        "propagates to every entry point in one place. "
                        "Parallel paths that bypass the graph and call "
                        "shared services directly are not permitted."
                    ),
                    "priority": "must-have",
                    "status": "in_progress",
                    "orphan": False,
                    "pytests": _orphan_pytest(),
                    "approval": "approved",
                    "type": "business",
                    "story_id": "ARCH-GRAPH-ORCHESTRATION-001",
                    "source": "human directive 2026-04-19",
                    "date": TODAY,
                },
                {
                    "req_id": "ARCH-GRAPH-ORCHESTRATION-001-REQ-T001",
                    "requirement": (
                        "Engineering rule v4-043 codifies the graph-"
                        "orchestration rule. preCommitScan.py gains a new "
                        "executor 'graph_entry_check' that AST-scans "
                        "specified scan_dirs for FastAPI/Flask route "
                        "handlers (functions decorated with @app.{post,"
                        "get,put,delete,patch} or @router.* equivalents) "
                        "whose body does not contain a call matching "
                        "<StateGraph_var>.invoke(...). Permitted "
                        "exceptions: handlers marked '# graph-exempt' AND "
                        "paired by a risk_acceptance.json entry. Existing "
                        "violator /classify (Code/ConversationalUX/"
                        "FindCareChat/backend/main.py:382) is tracked by "
                        "BUG-ARCH-GRAPH-001 and refactored under a "
                        "follow-on story. The scanner addition is tracked "
                        "by BUG-ARCH-GRAPH-002."
                    ),
                    "priority": "must-have",
                    "status": "in_progress",
                    "orphan": False,
                    "pytests": _orphan_pytest(),
                    "approval": "approved",
                    "type": "technical",
                    "story_id": "ARCH-GRAPH-ORCHESTRATION-001",
                    "source": "human directive 2026-04-19",
                    "date": TODAY,
                    "traces_to": "ARCH-GRAPH-ORCHESTRATION-001-REQ-B001",
                },
            ]}
        }]}
    })
    if isinstance(epic_008.get("features"), dict):
        epic_008["features"]["feature"] = feats
    else:
        epic_008["features"] = feats
    with open(BACKLOG, "w", encoding="utf-8") as f:
        json.dump(bl, f, indent=2)
    return True


# ── 3 + 4. Tracking bugs ──


def add_bugs():
    with open(BUGS, encoding="utf-8") as f:
        bg = json.load(f)
    bugs = bg.get("bug", [])
    existing = {b.get("bugid") for b in bugs}
    sev_by_env = {"dev": "critical", "qa": "show_stopper", "prod": "show_stopper"}

    if "BUG-ARCH-GRAPH-001" not in existing:
        bugs.append({
            "bugid": "BUG-ARCH-GRAPH-001",
            "title": ("/classify endpoint bypasses the LangGraph orchestrator "
                      "(violates v4-043)"),
            "description": (
                "Code/ConversationalUX/FindCareChat/backend/main.py:382 "
                "@app.post('/classify') handler calls "
                "_safety_service.is_emergency() and "
                "_specialty_service.find_specialty_codes() directly, "
                "bypassing the LangGraph orchestrator (_chat_graph) used "
                "by /chat. This is the parallel-path bug that lets a "
                "safety-classifier change silently break one path while "
                "the other looks fine. Manifested 2026-04-19 when the "
                "query 'I need a Shrink in DE' triggered a safety false-"
                "positive on /classify, returning {emergency: true} that "
                "the frontend mis-rendered as 'Could not identify "
                "relevant specialties.' Fix: refactor /classify to either "
                "(a) be a thin wrapper that invokes a subset of the "
                "graph (just classify + safety nodes), or (b) be "
                "deprecated with the frontend always going through "
                "/chat. Per v4-041 severity_by_environment intent: "
                "dev=critical, qa=show_stopper, prod=show_stopper."
            ),
            "environments": ["dev"],
            "status": "in_analysis",
            "severity": "critical",
            "req_id": "ARCH-GRAPH-ORCHESTRATION-001-REQ-T001",
            "story_id": "ARCH-GRAPH-ORCHESTRATION-001",
            "pytest_id": "TBD-refactor-pytest",
            "pytest_success_criteria": [
                "/classify route handler body contains _chat_graph.invoke(...) "
                "or equivalent graph entry call",
                "preCommitScan graph_entry_check passes for "
                "Code/ConversationalUX/FindCareChat/backend/main.py",
                "Same query through /chat and /classify produces consistent "
                "behavior across safety + classification."
            ],
            "discovery_date": TODAY,
            "due_date": "2026-05-15",
            "fix": "",
            "reproduction": (
                "1. POST {message: 'I need a Shrink in DE'} to "
                "https://localhost:7860/classify. "
                "2. Observe response: {emergency: true, response: 'Call 911...'}. "
                "3. POST same message to /chat. "
                "4. Observe whether /chat behaves consistently (likely also "
                "false-positives via shared safety_service, but routes "
                "through the graph)."
            ),
            "orphan": False,
            "next_action": [
                "Decide refactor approach for /classify: (a) graph subset "
                "wrapper, or (b) deprecate and route frontend through /chat."
            ],
        })

    if "BUG-ARCH-GRAPH-002" not in existing:
        bugs.append({
            "bugid": "BUG-ARCH-GRAPH-002",
            "title": ("preCommitScan has no executor for v4-043 "
                      "graph-orchestration enforcement"),
            "description": (
                "Engineering rule v4-043 declares enforcement type "
                "'graph_entry_check' but preCommitScan.py + "
                "pre_deploy_rule_check.py do not yet implement that "
                "executor. Until built, v4-043 is documentation-only "
                "and new violations will land at check-in undetected. "
                "The executor needs AST-level analysis (not pure regex) "
                "to detect FastAPI/Flask route handlers whose body does "
                "not invoke a StateGraph. Per v4-041 severity_by_"
                "environment intent: dev=low, qa=critical, "
                "prod=show_stopper."
            ),
            "environments": ["dev"],
            "status": "new",
            "severity": "low",
            "req_id": "ARCH-GRAPH-ORCHESTRATION-001-REQ-T001",
            "story_id": "ARCH-GRAPH-ORCHESTRATION-001",
            "pytest_id": "TBD-scanner-implementation",
            "pytest_success_criteria": [
                "preCommitScan.py EXECUTORS dict has 'graph_entry_check' key",
                "When run against a synthetic file containing a non-graph "
                "@app.post handler, the executor reports a violation",
                "When run against the same file with a graph.invoke() call, "
                "the executor reports clean"
            ],
            "discovery_date": TODAY,
            "due_date": "2026-05-15",
            "fix": "",
            "reproduction": (
                "Add a new @app.post handler that calls a service "
                "directly (bypassing any StateGraph) - commit succeeds "
                "without warning."
            ),
            "orphan": False,
            "next_action": [
                "Implement enforce_graph_entry_check(rule_id, enforcement) "
                "in preCommitScan.py + pre_deploy_rule_check.py using "
                "ast module."
            ],
        })

    bg["bug"] = bugs
    with open(BUGS, "w", encoding="utf-8") as f:
        json.dump(bg, f, indent=2)


def main():
    r = add_rule()
    b = add_backlog()
    add_bugs()
    print(f"v4-043 added: {r}")
    print(f"ARCH-GRAPH-ORCHESTRATION feature added: {b}")
    print("BUG-ARCH-GRAPH-001 + BUG-ARCH-GRAPH-002 filed")


if __name__ == "__main__":
    main()
