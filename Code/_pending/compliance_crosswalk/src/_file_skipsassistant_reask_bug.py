# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# One-shot apply: governance for the "agent re-asked permission for an
# in-scope deploy under active assignment" incident on 2026-04-19.
#
# What happened: Human assignment was "run local + dev deploy scripts."
# After completing the dev deploy, the agent asked "Want me to start the
# localhost deploy now?" before running deploy_localhost.sh, even though
# (a) deploy_localhost is within standing local authority per
# feedback_deploy_authority, and (b) the active assignment explicitly
# included the localhost deploy. The ask blocked execution and consumed
# Human time. Per v4-028 + v4-042, this re-ask is the bug.
#
# Files governance per human directive 2026-04-19:
#   1. Requirement: SKIP-ASSIST-001-REQ-T002 (no-reask-during-assignment)
#   2. Bug: BUG-SKIP-002 (critical) tracking the incident
#   3. Fix: documented in the requirement text (engineering directive on the
#      SkipsAssistant agent profile)
#   4. Pytest: ORPHAN placeholder (real test is behavioral / UAT-only)

from __future__ import annotations

import json
from pathlib import Path
from datetime import date

REPO = Path(r"c:/chatHealthy/findCare")
BACKLOG = REPO / "brain" / "machine_artifacts" / "content" / "agile_backlog.json"
BUGS = REPO / "brain" / "machine_artifacts" / "content" / "bugs.json"
TODAY = date.today().isoformat()


def _get(p, plural, singular):
    n = p.get(plural)
    return n.get(singular, []) if isinstance(n, dict) else (n if isinstance(n, list) else [])


def main():
    # ── 1 + 3) Add the new requirement (the fix is in its text) ──
    with open(BACKLOG, encoding="utf-8") as f:
        bl = json.load(f)
    e10 = next(e for e in bl["epics"]["epic"] if e.get("epic_id") == "EPIC-010")
    feats = _get(e10, "features", "feature")
    skips = next(f for f in feats if (f.get("feature_id") or f.get("id")) == "AGENT-SKIPS-ASSISTANT")
    story = next(s for s in _get(skips, "stories", "story")
                 if (s.get("story_id") or s.get("id")) == "SKIP-ASSIST-001")
    reqs = _get(story, "requirements", "requirement")
    existing_req_ids = {r.get("req_id") for r in reqs}

    if "SKIP-ASSIST-001-REQ-T002" not in existing_req_ids:
        reqs.append({
            "req_id": "SKIP-ASSIST-001-REQ-T002",
            "requirement": (
                "SkipsAssistant MUST NOT re-ask permission for any work "
                "step that is (a) clearly within standing authority "
                "(local + dev deploys per feedback_deploy_authority; reads "
                "anywhere; tool calls within Normal Mode default), AND "
                "(b) clearly inside the scope of an active assignment "
                "human has issued. Per v4-028, the directive IS the "
                "authorization. Per v4-039, blanket cascade covers child "
                "actions. Per v4-042, assignment scope overrides the "
                "ordinary 'ask-before-long-running-op' reflex. "
                "Acceptance: zero unprompted permission-asks during a "
                "named assignment when the next step is a script human "
                "explicitly named. If scope is genuinely ambiguous, "
                "SkipsAssistant asks ONCE to clarify scope, not for "
                "permission to act."
            ),
            "priority": "must-have",
            "status": "in_progress",
            "orphan": False,
            "pytests": {"pytest": [{
                "pytest_id": "TBD-behavioral-UAT-needed",
                "pytest_type": "ORPHAN",
                "orphan": True,
            }]},
            "approval": "approved",
            "type": "technical",
            "story_id": "SKIP-ASSIST-001",
            "source": ("human directive 2026-04-19 — incident: agent re-asked "
                       "permission for in-scope localhost deploy, blocking "
                       "assignment execution"),
            "date": TODAY,
            "traces_to": "SKIP-ASSIST-001-REQ-B001",
        })

    # ── Persist backlog ──
    if isinstance(story.get("requirements"), dict):
        story["requirements"]["requirement"] = reqs
    else:
        story["requirements"] = reqs
    with open(BACKLOG, "w", encoding="utf-8") as f:
        json.dump(bl, f, indent=2)

    # ── 2) File the critical tracking bug ──
    with open(BUGS, encoding="utf-8") as f:
        bg = json.load(f)
    bugs = bg.get("bug", [])
    if not any(b.get("bugid") == "BUG-SKIP-002" for b in bugs):
        bugs.append({
            "bugid": "BUG-SKIP-002",
            "title": ("Agent re-asked permission for in-scope localhost "
                      "deploy during active human assignment (critical)"),
            "description": (
                "human assignment 2026-04-19 was: 'run the local host and "
                "dev deployment scripts.' After dev deploy completed "
                "successfully, the agent asked 'Want me to start the "
                "localhost deploy now?' instead of just running it. The "
                "localhost deploy was clearly inside the named "
                "assignment scope and within standing local authority "
                "(feedback_deploy_authority), so the re-ask blocked "
                "execution and consumed human time. Human called this out "
                "as the rule-that-interfered-with-execution, instructed "
                "Claude to override via v4-042 assignment scope as one "
                "of that rule's enforcement mechanisms, and required "
                "this bug be filed at critical severity. The fix is "
                "encoded in SKIP-ASSIST-001-REQ-T002 (no-reask-during-"
                "assignment). Per v4-041 severity_by_environment intent: "
                "dev=critical, qa=show_stopper, prod=show_stopper "
                "(field will move to a structured property once the "
                "schema update ships to the published Cloudflare URL)."
            ),
            "environments": ["dev"],
            "status": "fixing",
            "severity": "critical",
            "req_id": "SKIP-ASSIST-001-REQ-T002",
            "story_id": "SKIP-ASSIST-001",
            "pytest_id": "TBD-behavioral-UAT-needed",
            "pytest_success_criteria": [
                "During an active named assignment, SkipsAssistant runs "
                "the next clearly in-scope deploy script without asking "
                "permission first.",
                "If scope is genuinely ambiguous, SkipsAssistant asks ONCE "
                "to clarify scope, not to re-authorize.",
                "Zero unprompted permission-asks observed across at least "
                "3 successive multi-step assignments under UAT."
            ],
            "discovery_date": TODAY,
            "due_date": "2026-04-26",
            "fix": ("SKIP-ASSIST-001-REQ-T002 added to the backlog under "
                    "AGENT-SKIPS-ASSISTANT/SKIP-ASSIST-001. The agent "
                    "profile now codifies that in-scope steps under an "
                    "active human assignment proceed without permission "
                    "asks. v4-042 (assignment scope > ordinary rule) is "
                    "the binding authority. Real validation is behavioral "
                    "UAT (human observation across multiple assignments)."),
            "reproduction": (
                "1. Human issues a multi-step assignment naming both step A "
                "and step B (e.g., 'run local + dev deploy scripts'). "
                "2. Claude completes step A. "
                "3. Before step B, Claude asks 'Want me to do step B now?' "
                "instead of just executing. "
                "Expected: Claude executes step B without asking."
            ),
            "orphan": False,
            "next_action": [
                "UAT during the next 3 multi-step assignments — verify "
                "no unprompted permission-asks for clearly in-scope steps."
            ],
        })
        bg["bug"] = bugs
        with open(BUGS, "w", encoding="utf-8") as f:
            json.dump(bg, f, indent=2)

    print("SKIP-ASSIST-001-REQ-T002 added to agile_backlog.json")
    print("BUG-SKIP-002 (critical) filed in bugs.json")
    print("Fix: documented in REQ-T002 text + this bug's fix field")
    print("Pytest: ORPHAN placeholder; real validation is behavioral UAT")


if __name__ == "__main__":
    main()
