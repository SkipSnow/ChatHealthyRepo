# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# generate_epic_plan_artifacts.py - Produces Word doc and JSON from plan_tree.json
# Layout: Front matter -> Feature bullet list -> Tree (Feature/Story/Req) -> Back matter (design)

import json
from pathlib import Path
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement

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


def repeat_header(table):
    try:
        tr = table.rows[0]._tr
        trPr = tr.get_or_add_trPr()
        trPr.append(OxmlElement("w:tblHeader"))
    except Exception:
        pass


def indented(text, inches, bold=False, size=10, color=None):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(inches)
    r = p.add_run(text)
    r.font.size = Pt(size)
    if bold:
        r.bold = True
    if color:
        r.font.color.rgb = RGBColor(*color)
    return p


# ════════════════════════════════════════════════════════════
# FRONT MATTER
# ════════════════════════════════════════════════════════════

# Title
doc.add_paragraph("")
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.add_run("ChatHealthy.ai").font.size = Pt(28)
p.runs[0].bold = True

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.add_run("Epic Plan: Evaluate Care v0.1.4").font.size = Pt(18)

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.add_run("April 2, 2026 | Build 363").font.size = Pt(11)

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = p.add_run(f"{total_f} Features | {total_s} Stories | {total_r} Requirements")
r.font.size = Pt(11)
r.font.color.rgb = RGBColor(11, 122, 117)

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.add_run("Copyright 2026 Skip Snow. All rights reserved.").font.size = Pt(9)

doc.add_page_break()

# How to Use
doc.add_heading("How to Use This Document", level=1)
doc.add_paragraph(
    "This document is organized in three sections:"
)
doc.add_paragraph("1. Feature List -- a scannable bullet list of every feature by name", style="List Number")
doc.add_paragraph("2. Plan Tree -- the full hierarchy: Feature -> Story -> Requirement -> Test Case", style="List Number")
doc.add_paragraph("3. Appendices -- software design, sprint map, risk matrix, rejected candidates", style="List Number")
doc.add_paragraph("")
doc.add_paragraph("The Plan Tree follows a strict indentation convention:")
doc.add_paragraph("Feature (flush left)", style="List Bullet")
doc.add_paragraph("Story (indented 0.25 inch)", style="List Bullet")
doc.add_paragraph("Requirement table (indented 0.5 inch)", style="List Bullet")
doc.add_paragraph("")
doc.add_paragraph(
    "All tables have headers that repeat at the top of every page. "
    "Requirement labels: Y (Passed), DEF (Deferred), FAIL (Failed). Blank until UAT."
)

doc.add_page_break()

# Executive Summary
doc.add_heading("Executive Summary", level=1)
doc.add_paragraph(
    f"This plan defines Evaluate Care v0.1.4 -- the second business component of ChatHealthy.ai. "
    f"It contains {total_f} features, {total_s} stories, and {total_r} boolean testable requirements. "
    f"Every feature was proposed by GPT (Enterprise Architect, gpt-5.3) and accepted by Claude "
    f"(Accountable Dev Manager, claude-opus-4-6). Every requirement traces to the epic goal."
)
doc.add_paragraph(f"Gate recommendation: {tree.get('gate_recommendation', 'N/A')}")

doc.add_page_break()

# ════════════════════════════════════════════════════════════
# FEATURE LIST (bullet list, names only)
# ════════════════════════════════════════════════════════════

doc.add_heading("Feature List", level=1)

for epic in tree["epics"]:
    features = epic.get("features", [])
    if not features:
        continue
    p = doc.add_paragraph()
    r = p.add_run(f"{epic['epic_id']}: {epic['name']}")
    r.bold = True
    r.font.size = Pt(12)

    for feat in features:
        doc.add_paragraph(
            f"{feat.get('feature_id', '?')}: {feat.get('name', '?')}",
            style="List Bullet"
        )

doc.add_page_break()

# ════════════════════════════════════════════════════════════
# PLAN TREE (Feature -> Story -> Requirement)
# ════════════════════════════════════════════════════════════

doc.add_heading("Plan Tree", level=1)

for epic in tree["epics"]:
    features = epic.get("features", [])
    if not features:
        continue

    doc.add_heading(f"{epic['epic_id']}: {epic['name']}", level=1)

    for feat in features:
        stories = feat.get("stories", [])
        feat_reqs = sum(len(s.get("requirements", [])) for s in stories)

        # ── Feature (flush left) ──
        p = doc.add_paragraph()
        r = p.add_run(f"Feature: {feat['feature_id']}: {feat['name']}")
        r.bold = True
        r.font.size = Pt(12)
        r.font.color.rgb = RGBColor(11, 122, 117)

        # Feature metadata as compact line
        layer = feat.get("layer", "?")
        priority = feat.get("priority", "?")
        accepted = feat.get("accepted_by", "?")
        doc.add_paragraph(
            f"Layer: {layer} | Priority: {priority} | Stories: {len(stories)} | "
            f"Requirements: {feat_reqs} | Accepted by: {accepted}"
        )
        evidence = feat.get("evidence", "")
        if evidence:
            p = doc.add_paragraph()
            r = p.add_run("Evidence: ")
            r.bold = True
            r.font.size = Pt(9)
            p.add_run(evidence[:200]).font.size = Pt(9)

        for story in stories:
            reqs = story.get("requirements", [])

            # ── Story (0.25in indent) ──
            indented(
                f"Story: {story['story_id']}: {story['title']}",
                0.25, bold=True, size=11
            )
            indented(story.get("description", ""), 0.25, size=9)
            indented(
                f"Size: {story.get('size', '?')} | Sprint: {story.get('sprint', '?')} | "
                f"Reqs: {len(reqs)}",
                0.25, size=9
            )
            if story.get("evidence"):
                indented(f"Evidence: {story['evidence'][:150]}", 0.25, size=8)
            if story.get("dependencies"):
                indented(f"Dependencies: {', '.join(story['dependencies'])}", 0.25, size=8)

            # ── Requirements table (0.5in indent via label) ──
            if reqs:
                indented("Requirements:", 0.5, bold=True, size=9)
                rt = doc.add_table(rows=1, cols=4)
                rt.style = "Table Grid"
                for i, h in enumerate(["Requirement ID", "Requirement", "Priority", "Label"]):
                    rt.rows[0].cells[i].text = h
                    rt.rows[0].cells[i].paragraphs[0].runs[0].bold = True
                    rt.rows[0].cells[i].paragraphs[0].runs[0].font.size = Pt(8)
                repeat_header(rt)
                for req in reqs:
                    row = rt.add_row()
                    row.cells[0].text = req.get("req_id", "?")
                    row.cells[1].text = req.get("requirement", "?")[:150]
                    row.cells[2].text = req.get("priority", "?")
                    row.cells[3].text = req.get("label", "")  # blank until UAT
                    for cell in row.cells:
                        cell.paragraphs[0].runs[0].font.size = Pt(8)

        doc.add_page_break()

# ════════════════════════════════════════════════════════════
# APPENDICES (back matter)
# ════════════════════════════════════════════════════════════

doc.add_heading("Appendix A: Software Design", level=1)

doc.add_heading("Data Flow", level=2)
for step in [
    "1. User asks about provider quality or clinical trial quality",
    "2. Claude Sonnet routes to evaluate_provider_quality() or evaluate_trial_quality()",
    "3. EvaluateCareFacade collects measures from provider/trial services",
    "4. Each measure normalized (0-1 scale)",
    "5. ScoringEngine computes weighted composite score (deterministic)",
    "6. ScoreExplainabilityService generates measure breakdown + provenance",
    "7. MeasureFailsafe validates minimum 3 measures",
    "8. Score card rendered in chat via EVAL-EXPLAIN-UX",
]:
    doc.add_paragraph(step)

doc.add_heading("AI Calls", level=2)
t = doc.add_table(rows=1, cols=3)
t.style = "Table Grid"
for i, h in enumerate(["Model", "Purpose", "Invocation"]):
    t.rows[0].cells[i].text = h
    t.rows[0].cells[i].paragraphs[0].runs[0].bold = True
repeat_header(t)
for m, p, inv in [
    ("Claude Sonnet 4.6", "Conversation + tool calling", "Per message"),
    ("Claude Haiku 4.5", "Query expansion, de-ID", "Per tool call"),
    ("GPT-4.1-mini", "Safety classification", "Per message"),
    ("text-embedding-3-large", "Provider vector search", "Per search"),
    ("Google Routes API", "Travel distance", "Per trial location"),
]:
    row = t.add_row()
    row.cells[0].text = m
    row.cells[1].text = p
    row.cells[2].text = inv

doc.add_heading("Object Model", level=2)
t = doc.add_table(rows=1, cols=4)
t.style = "Table Grid"
for i, h in enumerate(["Class", "Location", "Methods", "Description"]):
    t.rows[0].cells[i].text = h
    t.rows[0].cells[i].paragraphs[0].runs[0].bold = True
repeat_header(t)
for cls, loc, meth, desc in [
    ("EvaluateCareFacade", "application/facades/", "evaluate_provider_quality(), evaluate_trial_quality()", "Orchestrates scoring + explainability"),
    ("ProviderScoringEngine", "domain/evaluate_care_quality/", "compute_score(), apply_weights()", "Deterministic composite from provider measures"),
    ("ClinicalTrialScoringEngine", "domain/evaluate_care_quality/", "compute_score(), apply_weights()", "Deterministic composite from trial measures"),
    ("ScoreExplainabilityService", "domain/evaluate_care_quality/", "explain(), format_breakdown()", "Measure breakdowns + provenance"),
    ("MeasureNormalizationFramework", "domain/evaluate_care_quality/", "normalize(), handle_missing()", "0-1 normalization"),
    ("MeasureFailsafe", "domain/evaluate_care_quality/", "validate_minimum(), degrade_gracefully()", "Min 3 measures enforcement"),
    ("QualityScoreCache", "infrastructure/", "get(), put(), invalidate()", "Score caching"),
    ("DataProvenanceTracker", "infrastructure/", "attach(), trace()", "Data lineage per measure"),
]:
    row = t.add_row()
    row.cells[0].text = cls
    row.cells[1].text = loc
    row.cells[2].text = meth
    row.cells[3].text = desc

doc.add_page_break()

# Sprint Map
doc.add_heading("Appendix B: Sprint Capability Map", level=1)
for sprint in tree.get("sprint_map", []):
    if not isinstance(sprint, dict):
        continue
    doc.add_heading(f"Sprint {sprint.get('sprint', '?')}: {sprint.get('sprint_goal', '')}", level=2)
    doc.add_paragraph(f"Dates: {sprint.get('dates', '?')} | Capacity: {sprint.get('capacity_split', '?')}")
    shipped = sprint.get("features_shipped", [])
    if shipped:
        doc.add_paragraph(f"Features ({len(shipped)}): {', '.join(shipped[:10])}")
        if len(shipped) > 10:
            doc.add_paragraph(f"  ... +{len(shipped)-10} more")
    if sprint.get("notes"):
        doc.add_paragraph(f"Notes: {sprint['notes']}")

# Risk Matrix
doc.add_heading("Appendix C: Risk Matrix", level=1)
rm = tree.get("risk_matrix", {})
if isinstance(rm, dict):
    pf = rm.get("per_feature", [])
    if pf:
        doc.add_heading("Per Feature", level=2)
        t = doc.add_table(rows=1, cols=4)
        t.style = "Table Grid"
        for i, h in enumerate(["Feature", "Likelihood", "Impact", "Mitigation"]):
            t.rows[0].cells[i].text = h
            t.rows[0].cells[i].paragraphs[0].runs[0].bold = True
        repeat_header(t)
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
            t.rows[0].cells[i].paragraphs[0].runs[0].bold = True
        repeat_header(t)
        for e in pm:
            if isinstance(e, dict):
                row = t.add_row()
                row.cells[0].text = e.get("feature_id", "?")
                row.cells[1].text = e.get("data_source_availability", "?")
                row.cells[2].text = e.get("data_quality", "?")
                row.cells[3].text = e.get("credibility", "?")
                row.cells[4].text = e.get("mitigation", "")[:80]

# Rejected Candidates
rejected = tree.get("rejected_measure_candidates", [])
if rejected:
    doc.add_heading("Appendix D: Rejected Measure Candidates", level=1)
    doc.add_paragraph("Evaluated and rejected by Boss. Preserved for institutional knowledge.")
    t = doc.add_table(rows=1, cols=4)
    t.style = "Table Grid"
    for i, h in enumerate(["ID", "Name", "Disposition", "Reason"]):
        t.rows[0].cells[i].text = h
        t.rows[0].cells[i].paragraphs[0].runs[0].bold = True
    repeat_header(t)
    for r in rejected:
        row = t.add_row()
        row.cells[0].text = r.get("id", "?")
        row.cells[1].text = r.get("name", "?")
        row.cells[2].text = r.get("disposition", "?")
        row.cells[3].text = r.get("reason_dropped", "")[:120]

# Gate
doc.add_heading("Gate Recommendation", level=1)
doc.add_paragraph(f"Gate: {tree.get('gate_recommendation', 'N/A')}")

# Save
doc.save(str(_DOC_OUT))
print(f"Word: {_DOC_OUT}")

with open(_JSON_OUT, "w", encoding="utf-8") as f:
    json.dump(tree, f, indent=2, ensure_ascii=False)
print(f"JSON: {_JSON_OUT}")
print(f"\nTotals: {total_f} features, {total_s} stories, {total_r} requirements")
