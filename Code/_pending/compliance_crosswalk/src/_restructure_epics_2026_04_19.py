# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# One-shot apply: restructure per Boss directive 2026-04-19.
#
# Moves:
#   - Adds EPIC-002/SEC-COMPLIANCE-COMMITMENTS (parent business req for compliance trace).
#   - Adds EPIC-002/SEC-DEFERRED-WORK (no-silent-drop policy for deferred security work).
#   - Moves EPIC-008/ARCH-COMPLIANCE-CROSSWALK stories -> EPIC-002/SEC-DEFERRED-WORK.
#   - Moves EPIC-010/AGENT-NIST-800-53-COMPLIANCE stories -> EPIC-002/SEC-DEFERRED-WORK.
#   - Removes the now-empty ARCH-COMPLIANCE-CROSSWALK feature from EPIC-008.
#   - Removes the now-empty AGENT-NIST-800-53-COMPLIANCE feature from EPIC-010.
#   - Renames EPIC-010 "Agents" -> "Engineering Productivity"; updates epic-level description.
#   - Backfills traces_to on all moved + SkipsAssistant requirements.

from __future__ import annotations

import json
from pathlib import Path
from datetime import date

REPO = Path(r"c:/chatHealthy/findCare")
BACKLOG = REPO / "brain" / "machine_artifacts" / "content" / "agile_backlog.json"
TODAY = date.today().isoformat()


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


def make_req(req_id, type_, priority, requirement, story_id, traces_to=None):
    r = {
        "req_id": req_id,
        "requirement": requirement,
        "priority": priority,
        "status": "not_started",
        "orphan": False,
        "pytests": [],
        "approval": "approved",
        "type": type_,
        "story_id": story_id,
        "source": "Boss directive 2026-04-19 - restructure epics, close trace gap",
        "date": TODAY,
    }
    if traces_to:
        r["traces_to"] = traces_to
    return r


def main():
    with open(BACKLOG, encoding="utf-8") as f:
        bl = json.load(f)

    epics = bl["epics"]["epic"]
    epic_002 = next(e for e in epics if e.get("epic_id") == "EPIC-002"
                    and e.get("name") == "Security")
    epic_008 = next(e for e in epics if e.get("epic_id") == "EPIC-008"
                    and e.get("name") == "Architecture")
    epic_010 = next(e for e in epics if e.get("epic_id") == "EPIC-010")

    # ── 1) Rename EPIC-010 + update description ──
    epic_010["name"] = "Engineering Productivity"
    epic_010["description"] = (
        "ChatHealthy.ai engineering productivity is amplified by AI-assisted "
        "agents that execute well-scoped procedural assignments end-to-end, "
        "with cascading risk acceptance (v4-039), brain-aware authority, and "
        "transparent push-back. Direct anchor to the founding thesis: a "
        "1-3 person company plus a call center can deliver a major American "
        "brand. Agents that serve customers belong in their respective "
        "business epics; agents that serve the engineering function live here."
    )

    # ── 2) Build SEC-COMPLIANCE-COMMITMENTS feature ──
    sec_features = get_features(epic_002)

    if not any((f.get("feature_id") or f.get("id")) == "SEC-COMPLIANCE-COMMITMENTS"
               for f in sec_features):
        commit_feat = {
            "feature_id": "SEC-COMPLIANCE-COMMITMENTS",
            "name": "Compliance attestation surface",
            "status": "in_progress",
            "description": (
                "Closes the trace from compliance-implementation work back to "
                "ChatHealthy.ai stated security commitments in "
                "governance.json/security_standards and "
                "security.json/compliance_mapping. Healthcare enterprise "
                "buyers require HITRUST + NIST/SOC2 posture; this feature is "
                "the surface that proves we maintain it continuously."
            ),
            "stories": {"story": [
                {
                    "story_id": "SEC-COMPLIANCE-COMMITMENTS-001",
                    "name": "Continuous audit-readiness against committed frameworks",
                    "status": "in_progress",
                    "requirements": {"requirement": [
                        make_req(
                            "SEC-COMPLIANCE-COMMITMENTS-REQ-B001",
                            "business", "must-have",
                            "ChatHealthy.ai MUST maintain audit-readiness "
                            "against HITRUST CSF v11 and NIST SP 800-171 Rev 2 "
                            "(with NIST SP 800-53 Rev 5 as the implementation "
                            "surface that supersets 800-171), per the "
                            "commitments named in governance.json/"
                            "security_standards and security.json/"
                            "compliance_mapping. The audit surface MUST be "
                            "machine-readable, persisted in the brain, and "
                            "continuously regenerable from current code.",
                            "SEC-COMPLIANCE-COMMITMENTS-001"),
                        make_req(
                            "SEC-COMPLIANCE-COMMITMENTS-REQ-T001",
                            "technical", "must-have",
                            "An auditable control mapping (today: "
                            "brain/machine_artifacts/content/"
                            "SecurityAuditControls.json) MUST exist as a "
                            "single artifact persisted in the brain, "
                            "regenerable from current code via "
                            "NIST800-53ComplianceAgent (today located in "
                            "Code/_pending/compliance_crosswalk/ pending "
                            "promotion). The mapping MUST cover every "
                            "applicable NIST 800-53 control and HITRUST CSF "
                            "v11 control and bind each to backlog "
                            "requirements + source code blocks that "
                            "implement it.",
                            "SEC-COMPLIANCE-COMMITMENTS-001",
                            traces_to="SEC-COMPLIANCE-COMMITMENTS-REQ-B001"),
                    ]}
                }
            ]}
        }
        sec_features.append(commit_feat)

    # ── 3) Build SEC-DEFERRED-WORK feature (and move stories into it) ──
    if not any((f.get("feature_id") or f.get("id")) == "SEC-DEFERRED-WORK"
               for f in sec_features):
        deferred_feat = {
            "feature_id": "SEC-DEFERRED-WORK",
            "name": "Deferred security work (visible until completed)",
            "status": "in_progress",
            "description": (
                "Security work that is unfunded for the current sprint MUST "
                "remain visible, traceable, and severity-tracked until "
                "completed. No silent drop. Per v4-041 (deferred work "
                "tracking pattern), each story here has paired tracking "
                "bug(s) in bugs.json with severity_by_environment that "
                "escalates as the gap approaches production."
            ),
            "stories": {"story": []}
        }
        sec_features.append(deferred_feat)

    deferred_feat = next(f for f in sec_features
                         if (f.get("feature_id") or f.get("id")) == "SEC-DEFERRED-WORK")
    deferred_stories = deferred_feat["stories"]["story"]
    existing_story_ids = {s.get("story_id") or s.get("id")
                          for s in deferred_stories}

    # ── 4) MOVE ARCH-COMPLIANCE-CROSSWALK stories from EPIC-008 -> SEC-DEFERRED-WORK ──
    arch_features = get_features(epic_008)
    cw_idx = next((i for i, f in enumerate(arch_features)
                   if (f.get("feature_id") or f.get("id")) == "ARCH-COMPLIANCE-CROSSWALK"),
                  None)
    if cw_idx is not None:
        cw_feat = arch_features[cw_idx]
        for story in get_stories(cw_feat):
            sid = story.get("story_id") or story.get("id")
            if sid in existing_story_ids:
                continue
            # Backfill traces_to on every requirement in this story
            for r in _get(story, "requirements", "requirement"):
                r.setdefault("traces_to", "SEC-COMPLIANCE-COMMITMENTS-REQ-B001")
            # Re-parent the story
            story["parent_feature"] = "SEC-DEFERRED-WORK"
            deferred_stories.append(story)
            existing_story_ids.add(sid)
        # Remove the now-empty feature from EPIC-008
        arch_features.pop(cw_idx)
        set_features(epic_008, arch_features)

    # ── 5) MOVE AGENT-NIST-800-53-COMPLIANCE stories from EPIC-010 -> SEC-DEFERRED-WORK ──
    epic_010_features = get_features(epic_010)
    nist_idx = next((i for i, f in enumerate(epic_010_features)
                     if (f.get("feature_id") or f.get("id")) == "AGENT-NIST-800-53-COMPLIANCE"),
                    None)
    if nist_idx is not None:
        nist_feat = epic_010_features[nist_idx]
        for story in get_stories(nist_feat):
            sid = story.get("story_id") or story.get("id")
            if sid in existing_story_ids:
                continue
            for r in _get(story, "requirements", "requirement"):
                r.setdefault("traces_to", "SEC-COMPLIANCE-COMMITMENTS-REQ-B001")
            story["parent_feature"] = "SEC-DEFERRED-WORK"
            deferred_stories.append(story)
            existing_story_ids.add(sid)
        epic_010_features.pop(nist_idx)
        set_features(epic_010, epic_010_features)

    # ── 6) Backfill traces_to on SkipsAssistant reqs (point to epic-level intent) ──
    for f in get_features(epic_010):
        if (f.get("feature_id") or f.get("id")) == "AGENT-SKIPS-ASSISTANT":
            for st in get_stories(f):
                for r in _get(st, "requirements", "requirement"):
                    if r.get("req_id", "").startswith("SKIP-ASSIST-"):
                        r.setdefault("traces_to", "EPIC-010 (Engineering Productivity epic intent)")

    # Persist features back into EPIC-002
    set_features(epic_002, sec_features)

    with open(BACKLOG, "w", encoding="utf-8") as f:
        json.dump(bl, f, indent=2)

    # ── Verify ──
    with open(BACKLOG, encoding="utf-8") as f:
        bl2 = json.load(f)
    e2 = next(e for e in bl2["epics"]["epic"] if e.get("epic_id") == "EPIC-002")
    e8 = next(e for e in bl2["epics"]["epic"] if e.get("epic_id") == "EPIC-008")
    e10 = next(e for e in bl2["epics"]["epic"] if e.get("epic_id") == "EPIC-010")

    print("EPIC-002 Security features after restructure:")
    for f in get_features(e2):
        fid = f.get("feature_id") or f.get("id")
        n_stories = len(get_stories(f))
        marker = " (NEW)" if fid in ("SEC-COMPLIANCE-COMMITMENTS",
                                       "SEC-DEFERRED-WORK") else ""
        print(f"  - {fid}: {f.get('name','')[:50]} ({n_stories} stories){marker}")

    print()
    print("EPIC-008 Architecture features after restructure:")
    for f in get_features(e8):
        fid = f.get("feature_id") or f.get("id")
        print(f"  - {fid}: {f.get('name','')[:50]}")
    print(f"  (ARCH-COMPLIANCE-CROSSWALK present: {'ARCH-COMPLIANCE-CROSSWALK' in [(f.get('feature_id') or f.get('id')) for f in get_features(e8)]})")

    print()
    print(f"EPIC-010 renamed: name={e10.get('name')!r}")
    for f in get_features(e10):
        fid = f.get("feature_id") or f.get("id")
        print(f"  - {fid}: {f.get('name','')[:50]}")
    print(f"  (AGENT-NIST-800-53-COMPLIANCE present: {'AGENT-NIST-800-53-COMPLIANCE' in [(f.get('feature_id') or f.get('id')) for f in get_features(e10)]})")


if __name__ == "__main__":
    main()
