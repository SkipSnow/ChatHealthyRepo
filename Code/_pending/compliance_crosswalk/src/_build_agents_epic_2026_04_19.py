# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# One-shot apply: builds the new EPIC-010 "Agents" with two agents
# (SkipsAssistant, NIST800-53ComplianceAgent) per human directive
# 2026-04-19. Also files BUG-DEFER-005 tracking the SkipsAssistant
# codification as a new deferred unit.

from __future__ import annotations

import json
from pathlib import Path
from datetime import date

REPO = Path(r"c:/chatHealthy/findCare")
BACKLOG = REPO / "brain" / "machine_artifacts" / "content" / "agile_backlog.json"
BUGS = REPO / "brain" / "machine_artifacts" / "content" / "bugs.json"
TODAY = date.today().isoformat()


def make_req(req_id, type_, priority, requirement, story_id):
    return {
        "req_id": req_id,
        "requirement": requirement,
        "priority": priority,
        "status": "not_started",
        "orphan": False,
        "pytests": [],
        "approval": "approved",
        "type": type_,
        "story_id": story_id,
        "source": "human directive 2026-04-19 - BuildAnEpic Agents",
        "date": TODAY,
    }


def main():
    # ── Backlog: add new EPIC-010 Agents ──
    with open(BACKLOG, encoding="utf-8") as f:
        bl = json.load(f)

    if not any(e.get("epic_id") == "EPIC-010" for e in bl["epics"]["epic"]):
        epic = {
            "epic_id": "EPIC-010",
            "name": "Agents",
            "type": "functional",
            "status": "in_progress",
            "description": (
                "Catalog of named, governed agents that operate inside the "
                "ChatHealthy.ai project. Each agent is a software entity "
                "(local Python or model-driven) with a defined scope, "
                "inputs, outputs, public interface, and authority profile. "
                "Agents are the unit of operational responsibility above "
                "individual scripts. New agent = new feature under this epic."
            ),
            "features": {"feature": [
                # ── Feature 1: SkipsAssistant ──
                {
                    "feature_id": "AGENT-SKIPS-ASSISTANT",
                    "name": "SkipsAssistant",
                    "status": "in_progress",
                    "description": (
                        "Skips Assistant - the agent that helps Skip Snow "
                        "execute well-scoped procedural assignments inside "
                        "the ChatHealthy.ai project. Today realized by "
                        "Claude Code (Anthropic) configured by CLAUDE.md, "
                        ".claude/settings.json hooks, the engineering "
                        "rules brain, and a per-conversation memory layer. "
                        "Operates under cascading risk acceptance (v4-039) "
                        "and treats an active assignment as overriding "
                        "ordinary engineering rules per v4-042."
                    ),
                    "stories": {"story": [
                        {
                            "story_id": "SKIP-ASSIST-001",
                            "name": "Codify SkipsAssistant capabilities and authority profile",
                            "status": "in_progress",
                            "requirements": {"requirement": [
                                make_req(
                                    "SKIP-ASSIST-001-REQ-B001",
                                    "business", "must-have",
                                    "SkipsAssistant MUST be able to: (1) execute "
                                    "well-scoped procedural assignments end-to-end "
                                    "(brain edits, code refactors, file moves, doc "
                                    "generation, test runs); (2) propagate any new "
                                    "concept across all governed brain artifacts in "
                                    "one coherent change set; (3) honor every "
                                    "engineering rule unless the active assignment "
                                    "explicitly overrides specific ones (v4-042); "
                                    "(4) push back when Bosss idea is flawed (with "
                                    "evidence) before acting; (5) ring the bell "
                                    "before any pause that needs human input "
                                    "(v4-008); (6) measure load size before asking "
                                    "human to test boot/hook/CLAUDE.md changes.",
                                    "SKIP-ASSIST-001"),
                                make_req(
                                    "SKIP-ASSIST-001-REQ-T001",
                                    "technical", "must-have",
                                    "SkipsAssistant is realized by Claude Code "
                                    "(Anthropic) configured by: (a) CLAUDE.md at "
                                    "the project root carrying the binding boot "
                                    "directives; (b) .claude/settings.json hook "
                                    "graph that invokes "
                                    "Code/Shared/ops/tools/chathealthy_devops_boot.py "
                                    "on UserPromptSubmit, PreToolUse, Stop, "
                                    "SessionStart, SessionEnd; (c) the engineering "
                                    "rules brain (engineering_rules.json) loaded at "
                                    "boot; (d) per-conversation auto-memory under "
                                    "C:/Users/skips/.claude/projects/c--chatHealthy-findCare/memory/. "
                                    "Authority profile: Normal Mode (default per "
                                    "v4-004), with cascade-on-acceptance per v4-039 "
                                    "and assignment-scope override per v4-042. The "
                                    "agent has tools-use access via the Anthropic "
                                    "SDK and standing local + dev deploy authority.",
                                    "SKIP-ASSIST-001"),
                            ]}
                        }
                    ]}
                },
                # ── Feature 2: NIST800-53ComplianceAgent ──
                {
                    "feature_id": "AGENT-NIST-800-53-COMPLIANCE",
                    "name": "NIST800-53ComplianceAgent",
                    "status": "in_progress",
                    "description": (
                        "NIST 800-53 + HITRUST CSF v11 Compliance Crosswalk "
                        "Agent. A two-pass local Python agent that builds "
                        "and maintains brain/machine_artifacts/content/"
                        "SecurityAuditControls.json - a master crosswalk "
                        "binding every applicable external control to "
                        "backlog requirements and source code blocks that "
                        "implement it. Currently lives in Code/_pending/"
                        "compliance_crosswalk/src/ as deferred work."
                    ),
                    "stories": {"story": [
                        {
                            "story_id": "NIST-COMPLIANCE-AGENT-001",
                            "name": "Compliance crosswalk agent realization",
                            "status": "in_progress",
                            "requirements": {"requirement": [
                                make_req(
                                    "NIST-COMPLIANCE-AGENT-001-REQ-B001",
                                    "business", "must-have",
                                    "NIST800-53ComplianceAgent MUST surface, for any "
                                    "external compliance control (NIST SP 800-53 Rev "
                                    "5 + HITRUST CSF v11), the backlog requirements "
                                    "and source code that implement it. The "
                                    "crosswalk MUST be machine-readable, audit-"
                                    "presentable, and persisted in the brain. "
                                    "Realizes ARCH-COMPLIANCE-CROSSWALK-001-REQ-B001 "
                                    "at the agent abstraction.",
                                    "NIST-COMPLIANCE-AGENT-001"),
                                make_req(
                                    "NIST-COMPLIANCE-AGENT-001-REQ-T001",
                                    "technical", "must-have",
                                    "Realized by Code/_pending/compliance_crosswalk/"
                                    "src/compliance_crosswalk_agent.py - single "
                                    "class file with public surface __init__ / run "
                                    "/ ComplianceCrossWalk(FileType) / "
                                    "ComplianceCrosswalkReport. Pass 1 (deterministic) "
                                    "ingests NIST OSCAL JSON catalog and seeds "
                                    "HITRUST from brain/security.json. Pass 2 uses "
                                    "Anthropic SDK (Sonnet 4.6 default, prompt "
                                    "caching) to map source files to controls. "
                                    "Output: brain/machine_artifacts/content/"
                                    "SecurityAuditControls.json. Pytest suite mocks "
                                    "the LLM (23 tests). See "
                                    "ARCH-COMPLIANCE-CROSSWALK-001-REQ-T001 for the "
                                    "underlying technical realization.",
                                    "NIST-COMPLIANCE-AGENT-001"),
                            ]}
                        }
                    ]}
                },
            ]}
        }
        bl["epics"]["epic"].append(epic)

    with open(BACKLOG, "w", encoding="utf-8") as f:
        json.dump(bl, f, indent=2)

    # ── Bugs: add BUG-DEFER-005 tracking SkipsAssistant codification ──
    with open(BUGS, encoding="utf-8") as f:
        bg = json.load(f)
    bugs = bg.get("bug", [])
    existing_bids = {b.get("bugid") for b in bugs}

    if "BUG-DEFER-005" not in existing_bids:
        bugs.append({
            "bugid": "BUG-DEFER-005",
            "title": "Deferred: SkipsAssistant capabilities not yet codified end-to-end",
            "description": (
                "Auto-tracking debt bug for deferred requirement "
                "SKIP-ASSIST-001-REQ-T001. SkipsAssistant capabilities "
                "are in active use (this conversation is one) but not "
                "yet fully codified across CLAUDE.md, settings.json "
                "hooks, engineering rules, and memory in a way that "
                "any fresh Claude Code session boots into the same "
                "configured SkipsAssistant agent. Severity escalates "
                "by environment per v4-041."
            ),
            "environments": ["dev"],
            "status": "open",
            "resolution_status": "deferred",
            "severity": "low",
            "severity_by_environment": {"dev": "low", "qa": "critical",
                                          "prod": "show_stopper"},
            "req_id": "SKIP-ASSIST-001-REQ-T001",
            "story_id": "SKIP-ASSIST-001",
            "pytest_id": "",
            "pytest_success_criteria": [],
            "discovery_date": TODAY,
            "due_date": "2026-05-15",
            "fix": "",
            "reproduction": "N/A - tracking bug, not a defect.",
            "orphan": False,
            "next_action": ["Approve and complete SKIP-ASSIST-001-REQ-T001"],
        })
    bg["bug"] = bugs
    with open(BUGS, "w", encoding="utf-8") as f:
        json.dump(bg, f, indent=2)

    # ── Verify ──
    with open(BACKLOG, encoding="utf-8") as f:
        bl2 = json.load(f)
    epic_010 = next((e for e in bl2["epics"]["epic"]
                     if e.get("epic_id") == "EPIC-010"), None)
    if epic_010:
        feats = epic_010.get("features", {}).get("feature", [])
        print(f"EPIC-010 Agents created: {len(feats)} feature(s)")
        for ft in feats:
            stories = ft.get("stories", {}).get("story", [])
            for st in stories:
                reqs = st.get("requirements", {}).get("requirement", [])
                print(f"  {ft.get('feature_id')}/{st.get('story_id')}: "
                      f"{len(reqs)} requirement(s)")
    print(f"BUG-DEFER-005 added: {'BUG-DEFER-005' in {b.get('bugid') for b in bg['bug']}}")


if __name__ == "__main__":
    main()
