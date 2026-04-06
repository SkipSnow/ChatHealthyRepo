# Copyright (c) 2026 Skip Snow. All rights reserved.
import json, os

content_dir = "brain/machine_artifacts/content"

descriptions = {
    "ai_model_decisions": "Model selection history: GPT-4o to o3 to GPT-5.3. Rationale, SIT findings, retirement reasons.",
    "budget_config": "Token budget limits and cost thresholds for AI operations.",
    "gpt_qa_release_approval": "GPT formal GO/NO-GO verdict for production release. Conditions and risk assessment.",
    "gpt_reader_qa_report": "GPT Reader service SIT results. 27 tests, all requirements mapped.",
    "gpt_sit_finding": "FIND-SIT-001: GPT-4o cannot drive autonomous SIT. 3 attempts, 1M+ tokens, documented failure.",
    "gpt_sit_report": "GPT-5.3 SIT execution report. Live test results, bugs found, per-requirement verdicts.",
    "task_based_system_prompts": "Prompt templates for different GPT task types (architecture, QA, design).",
    "usage_log": "Token consumption log across all GPT/Claude API calls.",
    "gpt_assignment_system_prompt": "Static system prompt for GPT assignments. Prompt-cacheable. Includes brain manifest and output schema.",
    "component_discuss_care": "Discuss Care business component: 15 features, floor control, revenue model, moat, patent, governance rules.",
    "portability_refactoring_report": "Legacy: pre-ARCH-001 portability analysis. Superseded by refactor_design_arch001.",
    "refactor_design_arch001": "ARCH-001 approved design: 4-layer modular monolith, 2 facades, 9 services, 7-phase migration.",
    "refactor_design_final": "Legacy: Sprint 2 portability design. Superseded by refactor_design_arch001.",
    "refactor_proposal_iteration1": "GPT-5.3 initial design proposal. Input to ARCH-001 iteration process.",
    "refactor_proposal_iteration3": "GPT-5.3 revised proposal after Boss sign-off on 3 open questions.",
    "refactor_proposal_iteration5": "GPT-5.3 final proposal: migration plan, frontend, testing, entrypoint.",
    "refactor_results": "Legacy: portability refactoring execution results. Superseded.",
    "biz_arch_draft_v1": "Legacy: first GPT draft of business architecture. Superseded by v2.",
    "biz_arch_investor": "Legacy: first investor doc. Superseded by biz_arch_investor_v2.",
    "biz_arch_investor_v2": "Active investor business architecture. 3 components, revenue, moat, timeline. Maps to ChatHealthyOperationalBusinessArchitecture.docx.",
    "bizplan_cost_analysis_results": "GPT cost analysis output for business plan scenarios.",
    "business_model": "Core business model: Find Care (free) + Evaluate Care (paywall) + Discuss Care (premium tokens).",
    "staffing_cost_analysis": "Perplexity: staffing equivalency. Startup=$1.5M/9mo, Enterprise=$7M/18mo, AI-native=2wks. Maps to staffing_cost_comparison.docx.",
    "devops_build_promotion": "Build numbering, env-prefix routing, promotion pipeline (dev to qa to prod).",
    "support_tickets": "Support ticket log. Production incidents and resolutions.",
    "external_audit_registry": "Index of all external audits. Currently: EXT-AUDIT-001 (Perplexity, March 2026).",
    "external_audit_v1": "Perplexity full platform review: 18 findings, 0 critical, 3 high, 9 medium. Architecture validated.",
    "ai_governance_policy_draft": "AI governance policy: The model may suggest. The system must decide. Draft for formal adoption.",
    "corporate_governance": "GOV-001 through GOV-007. Provider neutrality, single-tenant, public-by-default, Boss sign-off.",
    "risk_acceptance_register": "Formal risks accepted by Boss. RISK-001: competitor trademarks in investor materials.",
    "safety_signoff": "Safety system sign-off record. Dual-trigger emergency detection validated.",
    "ip_agreement_inputs": "Input parameters for IP license agreement generation.",
    "ip_license_agreement": "FindCare Evaluation License (FEL-1.0) v11 draft. Pending legal review.",
    "ip_license_signoff": "IP license sign-off tracking. Status and approvals.",
    "pending_legal_questions": "Open legal questions: FDA classification, DeepSeek disclosure, consent wording.",
    "e2e_regression_report": "End-to-end regression test results. Pre-refactor baseline.",
    "production_release_status": "v0.1.3 production release status. Environment parity, deploy results, brain inventory.",
    "qa_report_v012": "v0.1.2 QA report: 13 features, 8 pass, 5 deferred, 48 builds.",
    "qa_report_v012_final": "v0.1.2 final QA markdown. Detailed feature-by-feature results.",
    "qa_report_v012_text": "v0.1.2 QA plain text export.",
    "qa_report_v013_release": "v0.1.3 RELEASE QA: 14 pass, 3 deferred, 0 fail. Boss+Claude+GPT GO.",
    "qa_report_v013_sprint1": "v0.1.3 Sprint 1 UAT report. Cycle 1 dev testing results.",
    "release_gate_v012": "v0.1.2 release gate decision. GPT-4o conditional-go.",
    "test_plan_results": "Test plan execution results. Unit + integration + SIT.",
    "backlog_0_1_4_perplexity": "10 backlog items from Perplexity audit for v0.1.4. Security, reliability, operations.",
    "backlog_brand_audit": "BRAND-001: audit all artifacts for ChatHealthy.ai (never ChatHealthy alone).",
    "consent_framework_feature": "Consent framework feature spec. Two-tier HIPAA flow.",
    "qa_report_feature": "QA report feature template. UAT report structure definition.",
    "sprint_plan_v013": "Sprint v0.1.3 plan: 7-phase refactor, maps, enhancements. Beta Sprint 1 epic (BRAIN-021).",
    "uat_library": "UAT scenario library. Reusable test cases across sprints.",
    "uat_v013": "UAT v0.1.3 feature list. 16 features with pass/fail/defer status.",
    "assignment_queue": "Pending GPT assignments. Brain runner reads from this queue.",
    "assurance_results": "GPT assurance output history. Architectural review results.",
    "biz_arch_collab_transcript": "Claude+GPT business architecture collaboration. 3 iterations.",
    "biz_arch_diagram_review": "GPT review of 5 business architecture diagrams.",
    "current_activity": "Current system activity and authorization mode.",
    "diagram_creative_direction": "GPT creative direction for Find Care + Evaluate Care diagrams.",
    "execution_state": "Brain runner execution state. Last run, pending items.",
    "refactor_collab_transcript": "ARCH-001 Claude+GPT design collaboration. 5 iterations, agreed design.",
    "review_queue": "Pending review items for GPT architect.",
    "session_2026_03_28_mind_dump": "Saturday March 28 testing session notes. Pre-refactor baseline.",
}

updated = 0
for fname in sorted(os.listdir(content_dir)):
    if not fname.endswith(".json") or fname in ("schema.json", "emergency_keywords.json", "funding_recommendation.json"):
        continue
    fpath = os.path.join(content_dir, fname)
    with open(fpath, "r", encoding="utf-8") as f:
        coll = json.load(f)
    changed = False
    for r in coll.get("records", []):
        rid = r.get("_record_id", "")
        if rid in descriptions:
            r["description"] = descriptions[rid]
            changed = True
            updated += 1
    if changed:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(coll, f, indent=2)

print(f"Updated {updated} record descriptions")

# Also fix the 2 orphan biz artifact mappings
biz_plan_path = os.path.join(content_dir, "business_plan.json")
with open(biz_plan_path, "r", encoding="utf-8") as f:
    bp = json.load(f)
for r in bp["records"]:
    if r.get("_record_id") == "biz_arch_investor_v2":
        r["business_artifact"] = "brain/BusinessArtifacts/ChatHealthyOperationalBusinessArchitecture.docx"
    if r.get("_record_id") == "staffing_cost_analysis":
        r["business_artifact"] = "brain/BusinessArtifacts/staffing_cost_comparison.docx"
with open(biz_plan_path, "w", encoding="utf-8") as f:
    json.dump(bp, f, indent=2)
print("Fixed 2 orphan biz artifact mappings")

# Remove duplicate pending_legal_questions in legal
legal_path = os.path.join(content_dir, "legal.json")
with open(legal_path, "r", encoding="utf-8") as f:
    leg = json.load(f)
seen = set()
deduped = []
for r in leg["records"]:
    rid = r.get("_record_id", "")
    if rid not in seen:
        seen.add(rid)
        deduped.append(r)
leg["records"] = deduped
leg["record_count"] = len(deduped)
with open(legal_path, "w", encoding="utf-8") as f:
    json.dump(leg, f, indent=2)
print(f"Legal deduped: {leg['record_count']} records")

# Update manifest
import hashlib, subprocess
result = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
tracked = result.stdout.strip().split("\n")
manifest = []
for rel_path in sorted(tracked):
    full = os.path.join(os.path.abspath("."), rel_path)
    if not os.path.isfile(full):
        continue
    try:
        size = os.path.getsize(full)
        with open(full, "rb") as f:
            content_hash = hashlib.sha256(f.read()).hexdigest()[:16]
    except:
        size, content_hash = 0, ""
    manifest.append({"project_path": rel_path, "size": size, "content_hash": content_hash})
with open("brain/machine_artifacts/content/project_manifest.json", "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
print(f"Manifest: {len(manifest)} files")
