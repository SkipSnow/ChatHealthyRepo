# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# generate_epic_plan_artifacts.py - Produces Word doc and JSON from plan_tree.json
# Includes: software design (flow, AI calls, object model), features, stories, requirements

import json
from pathlib import Path
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

_REPO = Path(__file__).parent.parent.parent.parent
_TREE = _REPO / "brain" / "machine_artifacts" / ".iteration_cache" / "plan_tree.json"
_DOC_OUT = _REPO / "brain" / "BusinessArtifacts" / "epic_plan_evaluate_care_v014.docx"
_JSON_OUT = _REPO / "brain" / "machine_artifacts" / ".iteration_cache" / "epic_plan_v014_final.json"

with open(_TREE, encoding="utf-8") as f:
    tree = json.load(f)

doc = Document()
style = doc.styles["Normal"]
style.font.name = "Calibri"
style.font.size = Pt(10)

total_f = sum(len(e.get("features", [])) for e in tree["epics"])
total_s = sum(len(f.get("stories", [])) for e in tree["epics"] for f in e.get("features", []))
total_r = sum(len(s.get("requirements", [])) for e in tree["epics"] for f in e.get("features", []) for s in f.get("stories", []))

# ── Title ──
doc.add_paragraph("")
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = p.add_run("ChatHealthy.ai")
r.font.size = Pt(28)
r.bold = True

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = p.add_run("Epic Plan: Evaluate Care v0.1.4")
r.font.size = Pt(18)

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.add_run("April 1, 2026 | Build 350").font.size = Pt(11)

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = p.add_run(f"{total_f} Features | {total_s} Stories | {total_r} Requirements")
r.font.size = Pt(11)
r.font.color.rgb = RGBColor(11, 122, 117)

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.add_run("Copyright 2026 Skip Snow. All rights reserved.").font.size = Pt(9)

doc.add_page_break()

# ── Exec Summary ──
doc.add_heading("Executive Summary", level=1)
doc.add_paragraph(
    f"This document defines the epic plan for Evaluate Care v0.1.4. "
    f"The plan contains {total_f} features, {total_s} stories, and {total_r} boolean "
    f"testable requirements. Every feature was proposed by GPT (Enterprise Architect, gpt-5.3) "
    f"and accepted by Claude (Accountable Dev Manager, claude-opus-4-6). "
    f"Every requirement traces to the epic goal."
)
doc.add_paragraph(f"Gate: {tree.get('gate_recommendation', 'N/A')}")

# ── Software Design ──
doc.add_heading("Software Design", level=1)

doc.add_heading("Data Flow", level=2)
flow = [
    "1. User asks about provider quality or clinical trial quality",
    "2. Claude Sonnet routes to evaluate_provider_quality() or evaluate_trial_quality()",
    "3. EvaluateCareFacade collects measures from provider/trial services",
    "4. Each measure normalized via MeasureNormalizationFramework (0-1)",
    "5. ScoringEngine computes weighted composite score (deterministic)",
    "6. ScoreExplainabilityService generates measure breakdown + provenance",
    "7. ConfidenceIndicator scores data completeness",
    "8. MeasureFailsafe validates minimum 3 measures; degrades if insufficient",
    "9. QualityScoreCache stores result",
    "10. EVAL-EXPLAIN-UX renders score card in chat",
]
for step in flow:
    doc.add_paragraph(step)

doc.add_heading("AI Calls", level=2)
t = doc.add_table(rows=1, cols=3)
t.style = "Table Grid"
for i, h in enumerate(["Model", "Purpose", "Invocation"]):
    t.rows[0].cells[i].text = h
for model, purpose, inv in [
    ("Claude Sonnet 4.6", "Conversation + tool calling", "Per message"),
    ("Claude Haiku 4.5", "Query expansion, summarization, de-ID", "Per tool call"),
    ("GPT-4.1-mini", "Safety classification", "Per message"),
    ("text-embedding-3-large", "Provider vector search", "Per search"),
    ("text-embedding-3-small", "Specialty vector search", "Per specialty"),
    ("Google Routes API", "Travel distance for trials", "Per trial location"),
]:
    row = t.add_row()
    row.cells[0].text = model
    row.cells[1].text = purpose
    row.cells[2].text = inv

doc.add_heading("Object Model", level=2)
objects = [
    ("EvaluateCareFacade", "application/facades/",
     "evaluate_provider_quality(), evaluate_trial_quality(), get_score_explanation()",
     "Orchestrates scoring, measures, explainability"),
    ("ProviderScoringEngine", "domain/evaluate_care_quality/",
     "compute_score(), normalize(), apply_weights()",
     "Deterministic composite from provider measures"),
    ("ClinicalTrialScoringEngine", "domain/evaluate_care_quality/",
     "compute_score(), normalize(), apply_weights()",
     "Deterministic composite from trial measures"),
    ("ScoreExplainabilityService", "domain/evaluate_care_quality/",
     "explain(), format_breakdown(), trace_to_data()",
     "Measure values, contributions, provenance"),
    ("MeasureNormalizationFramework", "domain/evaluate_care_quality/",
     "normalize(), scale_to_range(), handle_missing()",
     "Consistent 0-1 normalization"),
    ("QualityScoreCache", "infrastructure/",
     "get(), put(), invalidate(), ttl_check()",
     "Read-through/write-through cache"),
    ("MeasureFailsafe", "domain/evaluate_care_quality/",
     "validate_minimum(), degrade_gracefully(), flag_insufficient()",
     "Minimum measure count enforcement"),
    ("ConfidenceIndicator", "domain/evaluate_care_quality/",
     "compute_confidence(), explain_confidence()",
     "Confidence based on data completeness"),
    ("DataProvenanceTracker", "infrastructure/",
     "attach(), trace(), audit_trail()",
     "Source, freshness, lineage per measure"),
]
t = doc.add_table(rows=1, cols=4)
t.style = "Table Grid"
for i, h in enumerate(["Class", "Location", "Methods", "Description"]):
    t.rows[0].cells[i].text = h
for cls, loc, methods, desc in objects:
    row = t.add_row()
    row.cells[0].text = cls
    row.cells[1].text = loc
    row.cells[2].text = methods
    row.cells[3].text = desc

doc.add_page_break()

# ── Features / Stories / Requirements ──
for epic in tree["epics"]:
    features = epic.get("features", [])
    if not features:
        continue

    doc.add_heading(f"{epic['epic_id']}: {epic['name']}", level=1)

    for feat in features:
        stories = feat.get("stories", [])
        feat_reqs = sum(len(s.get("requirements", [])) for s in stories)

        doc.add_heading(f"{feat['feature_id']}: {feat['name']}", level=2)

        t = doc.add_table(rows=6, cols=2)
        t.style = "Table Grid"
        for i, (k, v) in enumerate([
            ("Layer", feat.get("layer", "?")),
            ("Priority", feat.get("priority", "?")),
            ("Evidence", str(feat.get("evidence", ""))[:200]),
            ("Accepted by", feat.get("accepted_by", "?")),
            ("Stories", str(len(stories))),
            ("Requirements", str(feat_reqs)),
        ]):
            t.rows[i].cells[0].text = k
            t.rows[i].cells[1].text = v

        for story in stories:
            reqs = story.get("requirements", [])
            doc.add_heading(f"{story['story_id']}: {story['title']}", level=3)
            doc.add_paragraph(story.get("description", ""))
            doc.add_paragraph(
                f"Size: {story.get('size', '?')} | "
                f"Sprint: {story.get('sprint', '?')} | "
                f"Reqs: {len(reqs)}"
            )
            if story.get("evidence"):
                doc.add_paragraph(f"Evidence: {story['evidence'][:150]}")
            if story.get("dependencies"):
                doc.add_paragraph(f"Dependencies: {', '.join(story['dependencies'])}")

            if reqs:
                rt = doc.add_table(rows=1, cols=4)
                rt.style = "Table Grid"
                for i, h in enumerate(["ID", "Requirement", "Priority", "Status"]):
                    rt.rows[0].cells[i].text = h
                for req in reqs:
                    row = rt.add_row()
                    row.cells[0].text = req.get("req_id", "?")
                    row.cells[1].text = req.get("requirement", "?")[:150]
                    row.cells[2].text = req.get("priority", "?")
                    row.cells[3].text = req.get("status", "?")

        doc.add_page_break()

# ── Sprint Map ──
doc.add_heading("Sprint Capability Map", level=1)
for sprint in tree.get("sprint_map", []):
    if not isinstance(sprint, dict):
        continue
    doc.add_heading(f"Sprint {sprint.get('sprint', '?')}: {sprint.get('sprint_goal', '')}", level=2)
    doc.add_paragraph(f"Capacity: {sprint.get('capacity_split', '?')}")
    shipped = sprint.get("features_shipped", [])
    if shipped:
        doc.add_paragraph(f"Features: {', '.join(shipped)}")
    sprint_stories = sprint.get("stories", [])
    if sprint_stories:
        doc.add_paragraph(f"Stories: {', '.join(sprint_stories)}")

# ── Risk Matrix ──
doc.add_heading("Risk Matrix", level=1)
rm = tree.get("risk_matrix", {})
if isinstance(rm, dict):
    pf = rm.get("per_feature", [])
    if pf:
        doc.add_heading("Per Feature", level=2)
        t = doc.add_table(rows=1, cols=4)
        t.style = "Table Grid"
        for i, h in enumerate(["Feature", "Likelihood", "Impact", "Mitigation"]):
            t.rows[0].cells[i].text = h
        for e in pf:
            if isinstance(e, dict):
                row = t.add_row()
                row.cells[0].text = e.get("feature_id", "?")
                row.cells[1].text = e.get("likelihood", "?")
                row.cells[2].text = e.get("impact", "?")
                row.cells[3].text = e.get("mitigation", "")[:100]

    pm = rm.get("per_measure", [])
    if pm:
        doc.add_heading("Per Measure", level=2)
        t = doc.add_table(rows=1, cols=5)
        t.style = "Table Grid"
        for i, h in enumerate(["Measure", "Availability", "Quality", "Credibility", "Mitigation"]):
            t.rows[0].cells[i].text = h
        for e in pm:
            if isinstance(e, dict):
                row = t.add_row()
                row.cells[0].text = e.get("feature_id", "?")
                row.cells[1].text = e.get("data_source_availability", "?")
                row.cells[2].text = e.get("data_quality", "?")
                row.cells[3].text = e.get("credibility", "?")
                row.cells[4].text = e.get("mitigation", "")[:80]

# ── Gate ──
doc.add_heading("Gate Recommendation", level=1)
doc.add_paragraph(f"Gate: {tree.get('gate_recommendation', 'N/A')}")

# Save
doc.save(str(_DOC_OUT))
print(f"Word: {_DOC_OUT}")

# JSON
with open(_JSON_OUT, "w", encoding="utf-8") as f:
    json.dump(tree, f, indent=2, ensure_ascii=False)
print(f"JSON: {_JSON_OUT}")

print(f"\nTotals: {total_f} features, {total_s} stories, {total_r} requirements")
