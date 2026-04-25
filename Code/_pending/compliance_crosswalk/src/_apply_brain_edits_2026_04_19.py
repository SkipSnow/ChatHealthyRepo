# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# One-shot apply script for the four brain edits human approved 2026-04-19.
# After it runs successfully, this file may be deleted (it has no future caller).

from __future__ import annotations

import json
from pathlib import Path
from datetime import date

REPO = Path(r"c:/chatHealthy/findCare")
BACKLOG = REPO / "brain" / "machine_artifacts" / "content" / "agile_backlog.json"
BUGS = REPO / "brain" / "machine_artifacts" / "content" / "bugs.json"
RULES = REPO / "brain" / "machine_artifacts" / "content" / "engineering_rules.json"
BUG_SCHEMA = REPO / "Website" / "schemas" / "dev" / "ChatHealthyBugsSchema.json"
TODAY = date.today().isoformat()


# ── shape helpers (backlog tree is plural-singular per v4-032) ────


def _get(parent, plural, singular):
    n = parent.get(plural)
    if isinstance(n, dict):
        return n.get(singular, [])
    return n if isinstance(n, list) else []


def _set(parent, plural, singular, new_list):
    if isinstance(parent.get(plural), dict):
        parent[plural][singular] = new_list
    else:
        parent[plural] = new_list


def get_features(epic):    return _get(epic, "features", "feature")
def set_features(epic, l): _set(epic, "features", "feature", l)
def get_stories(feat):     return _get(feat, "stories", "story")
def set_stories(feat, l):  _set(feat, "stories", "story", l)
def get_reqs(story):       return _get(story, "requirements", "requirement")
def set_reqs(story, l):    _set(story, "requirements", "requirement", l)


def make_req(req_id, type_, priority, requirement, story_id):
    return {
        "req_id": req_id,
        "requirement": requirement,
        "priority": priority,
        "status": "not_started",
        "orphan": False,
        "pytests": [],
        "approval": "proposed",
        "type": type_,
        "story_id": story_id,
        "source": "human directive 2026-04-19 — overnight compliance crosswalk + deferral pattern",
        "date": TODAY,
    }


# ── EDIT 1 — agile_backlog.json ──


def edit_1_backlog():
    with open(BACKLOG, encoding="utf-8") as f:
        bl = json.load(f)
    epic = next(e for e in bl["epics"]["epic"]
                if e.get("epic_id") == "EPIC-008" and e.get("name") == "Architecture")
    features = get_features(epic)

    # Slot 1 — existing ARCH-BRAIN / BRAIN-ENGINEERING-RULES story
    arch_brain = next(f for f in features
                      if (f.get("feature_id") or f.get("id")) == "ARCH-BRAIN")
    ber = next(s for s in get_stories(arch_brain)
               if (s.get("story_id") or s.get("id")) == "BRAIN-ENGINEERING-RULES")
    reqs = get_reqs(ber)
    existing = {r.get("req_id") for r in reqs}
    additions = [
        make_req("EPIC-008-F-007-S-011-REQ-B-001", "business", "must-have",
                 "engineering_rules.json MUST express each rule as (a) a short "
                 "name (max 80 chars), (b) a non-binding description, and (c) "
                 "one or more individually Boolean-testable statements. Rule "
                 "identifiers MUST be uniform Rule-NNN. Every rule MUST carry "
                 "a date.",
                 "BRAIN-ENGINEERING-RULES"),
        make_req("EPIC-008-F-007-S-011-REQ-T-002", "technical", "must-have",
                 "Publish a strict JSON schema (Draft 2020-12) defining "
                 "rules[].rule with fields {id (matching ^Rule-\\d{3}$), name "
                 "(maxLength 80), description, rule_statements[] of "
                 "{statement_number >=1, statement_body}, date, type (enum "
                 "standard|incident-driven), source}. Migrate engineering_"
                 "rules.json in one commit; in the same commit rename every "
                 "v4-NNN/DR-NNN to Rule-NNN and update every reference in non-"
                 "loose-schema brain JSONs and bugs.json. preCommitScan.py "
                 "iterates rule_statements[] instead of the prose body.",
                 "BRAIN-ENGINEERING-RULES"),
    ]
    for a in additions:
        if a["req_id"] not in existing:
            reqs.append(a)
    set_reqs(ber, reqs)

    # Slot 2 + 3 — new feature ARCH-COMPLIANCE-CROSSWALK with 2 stories
    if not any((f.get("feature_id") or f.get("id")) == "ARCH-COMPLIANCE-CROSSWALK"
               for f in features):
        cw = {
            "feature_id": "ARCH-COMPLIANCE-CROSSWALK",
            "name": "Compliance Control Crosswalk Agent",
            "status": "in_progress",
            "stories": {"story": [
                {
                    "story_id": "ARCH-COMPLIANCE-CROSSWALK-001",
                    "name": "Build the compliance crosswalk agent (NIST 800-53 + HITRUST CSF v11)",
                    "status": "in_progress",
                    "requirements": {"requirement": [
                        make_req("EPIC-002-F-002-S-002-REQ-B-001",
                                 "business", "must-have",
                                 "The system MUST be auditable against NIST SP "
                                 "800-53 Rev 5 and HITRUST CSF v11 by maintaining "
                                 "a control crosswalk that binds every applicable "
                                 "control to backlog requirements and source code "
                                 "that implement it. The crosswalk MUST be "
                                 "machine-readable and persisted in the brain.",
                                 "ARCH-COMPLIANCE-CROSSWALK-001"),
                        make_req("EPIC-002-F-002-S-002-REQ-T-001",
                                 "technical", "must-have",
                                 "An agent (compliance_crosswalk_agent.py — under "
                                 "Code/_pending/compliance_crosswalk/src/, single "
                                 "class file, public surface __init__/run/"
                                 "ComplianceCrossWalk(FileType)/"
                                 "ComplianceCrosswalkReport) maintains "
                                 "brain/machine_artifacts/content/"
                                 "SecurityAuditControls.json. Pass 1 ingests NIST "
                                 "OSCAL + seeds HITRUST from security.json. Pass 2 "
                                 "uses Anthropic SDK (Sonnet 4.6, prompt caching) "
                                 "to map source files to controls. Pytest suite "
                                 "mocks the LLM. Output validates against the "
                                 "relaxed schema.",
                                 "ARCH-COMPLIANCE-CROSSWALK-001"),
                    ]}
                },
                {
                    "story_id": "ARCH-COMPLIANCE-CROSSWALK-002",
                    "name": "Crosswalk agent: third framework — engineering rules",
                    "status": "not_started",
                    "requirements": {"requirement": [
                        make_req("EPIC-002-F-003-S-002-REQ-B-001",
                                 "business", "should-have",
                                 "The system MUST surface, for any source file or "
                                 "any external compliance control, the internal "
                                 "engineering rules that govern it — in the same "
                                 "crosswalk.",
                                 "ARCH-COMPLIANCE-CROSSWALK-002"),
                        make_req("EPIC-002-F-003-S-002-REQ-T-001",
                                 "technical", "should-have",
                                 "Compliance crosswalk agent gains a third "
                                 "framework: engineering_rules. Pass 1 ingests "
                                 "engineering_rules.json (post-redesign) using "
                                 "rule_statements[] as the unit. Pass 2 extends "
                                 "the classifier so each implementation entry can "
                                 "reference both external control IDs and "
                                 "engineering rule IDs. Hard dependency on "
                                 "EPIC-008-F-007-S-011-REQ-T-002.",
                                 "ARCH-COMPLIANCE-CROSSWALK-002"),
                    ]}
                }
            ]}
        }
        features.append(cw)

    # Slot 4 — new feature BRAIN-GOVERNANCE-DEFERRAL-PATTERN
    if not any((f.get("feature_id") or f.get("id")) == "BRAIN-GOVERNANCE-DEFERRAL-PATTERN"
               for f in features):
        df = {
            "feature_id": "BRAIN-GOVERNANCE-DEFERRAL-PATTERN",
            "name": "Deferred work as a first-class concept",
            "status": "not_started",
            "stories": {"story": [
                {
                    "story_id": "BRAIN-DEFERRED-WORK-001",
                    "name": "End-to-end deferred-work tracking pattern",
                    "status": "not_started",
                    "requirements": {"requirement": [
                        make_req("EPIC-008-F-009-S-001-REQ-B-001",
                                 "business", "must-have",
                                 "Every unit of work that is deferred MUST be "
                                 "visible end-to-end: the deferred state, the "
                                 "requirement awaiting completion, and an auto-"
                                 "tracking bug that escalates severity as the "
                                 "un-completed work approaches production. "
                                 "Deferral remains visible until the requirement "
                                 "is fulfilled.",
                                 "BRAIN-DEFERRED-WORK-001"),
                        make_req("EPIC-008-F-009-S-001-REQ-T-001",
                                 "technical", "must-have",
                                 "Backlog requirements with status=deferred MUST "
                                 "have a paired tracking bug in bugs.json with "
                                 "resolution_status=deferred and severity_by_"
                                 "environment={dev: low, qa: critical, prod: "
                                 "show_stopper}. The unit's inputs, outputs, src, "
                                 "and a README MUST live under "
                                 "Code/_pending/<unit-name>/. A BugManagerAgent "
                                 "(future) auto-escalates severity on environment "
                                 "promotion. Engineering rule v4-041 (to become "
                                 "Rule-NNN post-redesign) codifies the pattern.",
                                 "BRAIN-DEFERRED-WORK-001"),
                    ]}
                }
            ]}
        }
        features.append(df)

    set_features(epic, features)
    with open(BACKLOG, "w", encoding="utf-8") as f:
        json.dump(bl, f, indent=2)
    return 8


# ── EDIT 2 — bugs.json ──


def edit_2_bugs():
    with open(BUGS, encoding="utf-8") as f:
        bg = json.load(f)
    bugs = bg.get("bug", [])
    existing = {b.get("bugid") for b in bugs}

    sev_by_env = {"dev": "low", "qa": "critical", "prod": "show_stopper"}

    def make_bug(bid, req_id, title):
        return {
            "bugid": bid,
            "title": title,
            "description": (f"Auto-tracking debt bug for deferred requirement "
                            f"{req_id}. Severity escalates by environment per "
                            f"v4-041 (Deferred work tracking pattern). Closes "
                            f"only when the requirement is completed and the "
                            f"work is moved out of Code/_pending/."),
            "environments": ["dev"],
            "status": "open",
            "resolution_status": "deferred",
            "severity": "low",
            "severity_by_environment": sev_by_env,
            "req_id": req_id,
            "story_id": req_id.rsplit("-REQ", 1)[0],
            "pytest_id": "",
            "pytest_success_criteria": [],
            "discovery_date": TODAY,
            "due_date": "2026-05-15",
            "fix": "",
            "reproduction": "N/A — tracking bug, not a defect.",
            "orphan": False,
            "next_action": [f"Approve and complete {req_id}"],
        }

    additions = [
        make_bug("BUG-DEFER-001", "EPIC-002-F-002-S-002-REQ-T-001",
                 "Deferred: compliance crosswalk agent not yet approved into prod"),
        make_bug("BUG-DEFER-002", "EPIC-008-F-007-S-011-REQ-T-002",
                 "Deferred: engineering_rules.json schema redesign not yet done"),
        make_bug("BUG-DEFER-003", "EPIC-002-F-003-S-002-REQ-T-001",
                 "Deferred: crosswalk third-framework (engineering rules) not yet done"),
        make_bug("BUG-DEFER-004", "EPIC-008-F-009-S-001-REQ-T-001",
                 "Deferred: deferred-work pattern not yet codified end-to-end"),
    ]
    for a in additions:
        if a["bugid"] not in existing:
            bugs.append(a)
    bg["bug"] = bugs
    with open(BUGS, "w", encoding="utf-8") as f:
        json.dump(bg, f, indent=2)
    return len(additions)


# ── EDIT 3 — bug schema (additive) ──


def edit_3_bug_schema():
    with open(BUG_SCHEMA, encoding="utf-8") as f:
        s = json.load(f)
    sev_field = {
        "type": "object",
        "description": ("Optional. Severity-per-environment for deferred-work "
                        "tracking bugs (engineering rule v4-041 / "
                        "BRAIN-DEFERRED-WORK-001). Lets a single bug encode "
                        "that the same gap is low in dev, critical in qa, "
                        "show_stopper in prod."),
        "properties": {
            "dev":  {"type": "string", "enum": ["show_stopper", "critical",
                                                  "high", "medium", "low",
                                                  "not_a_bug", "resolved"]},
            "qa":   {"type": "string", "enum": ["show_stopper", "critical",
                                                  "high", "medium", "low",
                                                  "not_a_bug", "resolved"]},
            "prod": {"type": "string", "enum": ["show_stopper", "critical",
                                                  "high", "medium", "low",
                                                  "not_a_bug", "resolved"]},
        },
        "additionalProperties": False,
    }
    bug_props = (s.get("properties", {}).get("bug", {})
                  .get("items", {}).get("properties"))
    if bug_props is not None:
        bug_props.setdefault("severity_by_environment", sev_field)
    else:
        # tolerant fallback if schema shape differs
        s.setdefault("properties", {})["severity_by_environment"] = sev_field
    with open(BUG_SCHEMA, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    return True


# ── EDIT 4 — engineering_rules.json ──


def edit_4_rules():
    with open(RULES, encoding="utf-8") as f:
        er = json.load(f)
    rules = er.get("rules", [])
    existing = {r.get("id") for r in rules}

    new_rules = []
    if "v4-041" not in existing:
        new_rules.append({
            "id": "v4-041",
            "title": "Deferred work tracking pattern",
            "rule": ("Every unit of work that is deferred MUST be tracked "
                     "end-to-end. (1) The unit MUST have at least one "
                     "requirement in agile_backlog.json with status=deferred. "
                     "(2) For each such requirement, an auto-tracking bug MUST "
                     "exist in bugs.json with resolution_status=deferred and "
                     "severity_by_environment={dev: low, qa: critical, "
                     "prod: show_stopper}. (3) The unit's input artifacts, "
                     "output artifacts, src, and a README MUST live in a self-"
                     "contained directory under Code/_pending/<unit_name>/. "
                     "(4) The tracking bug closes only when the requirement is "
                     "fulfilled AND the work is moved out of Code/_pending/."),
            "type": "standard",
            "date": TODAY,
            "source": "human directive 2026-04-19 — deferred work as first-class concept",
            "enforcement": [],
        })
    if "v4-042" not in existing:
        new_rules.append({
            "id": "v4-042",
            "title": "Assignment scope is higher authority than ordinary engineering rule",
            "rule": ("A human-issued ASSIGNMENT (a stated unit of work, with "
                     "intent to complete it) carries a higher scope of "
                     "authority than any ordinary engineering rule. When "
                     "applying engineering rules during execution, Claude "
                     "MUST treat an active assignment as overriding any rule "
                     "whose enforcement would block completion of the "
                     "assignment, EXCEPT for: GOV-007 (production sign-off), "
                     "v4-006 (deploy authority — QA/prod sign-off), and any "
                     "rule explicitly marked as universally enforced (e.g., "
                     "v4-026 governance matrix changes still require human "
                     "risk acceptance even mid-assignment). Assignment scope "
                     "MUST be logged in risk_acceptance.json once at the "
                     "start of the assignment, not per child action (per "
                     "v4-039 cascade)."),
            "type": "standard",
            "date": TODAY,
            "source": "human directive 2026-04-19 — assignment > ordinary rule",
            "enforcement": [],
        })

    rules.extend(new_rules)
    er["rules"] = rules
    with open(RULES, "w", encoding="utf-8") as f:
        json.dump(er, f, indent=2)
    return len(new_rules)


# ── Main ──


def main():
    n1 = edit_1_backlog()
    n2 = edit_2_bugs()
    edit_3_bug_schema()
    n4 = edit_4_rules()
    print(f"EDIT 1 — agile_backlog.json: 8 requirements added (3 stories, 2 new features)")
    print(f"EDIT 2 — bugs.json: {n2} tracking bugs added")
    print(f"EDIT 3 — ChatHealthyBugsSchema.json: severity_by_environment field added")
    print(f"EDIT 4 — engineering_rules.json: {n4} new rules added (v4-041, v4-042)")


if __name__ == "__main__":
    main()
