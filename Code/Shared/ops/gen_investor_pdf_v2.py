# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
import json, os
from fpdf import FPDF
from fpdf.enums import XPos, YPos

with open("brain/machine_artifacts/document_type_json/biz_arch_investor_v2.json", "r", encoding="utf-8") as f:
    data = json.load(f)

def safe(s):
    if not isinstance(s, str): s = str(s)
    for k, v in {"\u2014":"--","\u2013":"-","\u2018":"'","\u2019":"'","\u201c":'"',"\u201d":'"',"\u2022":"-","\u2026":"...","\u00a0":" ","\u2192":"->","\u2190":"<-"}.items():
        s = s.replace(k, v)
    return s.encode("latin-1", errors="replace").decode("latin-1")

pdf = FPDF(orientation="P", format="letter")
pdf.set_auto_page_break(auto=True, margin=20)

# Title
pdf.add_page()
pdf.ln(30)
pdf.set_font("Helvetica", "B", 32)
pdf.cell(0, 14, "ChatHealthy.ai", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
pdf.ln(4)
pdf.set_font("Helvetica", "B", 16)
pdf.cell(0, 10, "Find Care. Evaluate Care. Discuss Care.", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
pdf.ln(6)
pdf.set_font("Helvetica", "", 11)
pdf.cell(0, 8, "Business Architecture", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
pdf.ln(15)
pdf.set_font("Helvetica", "I", 11)
pdf.multi_cell(0, 6, safe(data.get("vision", "")), align="C")
pdf.ln(15)
pdf.set_font("Helvetica", "", 9)
pdf.cell(0, 6, "Copyright 2026 Skip Snow. All rights reserved.", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

# Exec summary
pdf.add_page()
pdf.set_font("Helvetica", "B", 18)
pdf.cell(0, 10, "Executive Summary", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
pdf.ln(3)
pdf.set_font("Helvetica", "", 10)
es = data.get("executive_summary", "")
text = "\n\n".join(es) if isinstance(es, list) else str(es)
for p in text.split("\n\n"):
    pdf.multi_cell(0, 5, safe(p))
    pdf.ln(3)

# Components
for key in ["find_care", "evaluate_care", "discuss_care"]:
    comp = data.get("components", {}).get(key, {})
    if not comp: continue
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, safe(comp.get("name", key)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if comp.get("tagline"):
        pdf.set_font("Helvetica", "I", 12)
        pdf.cell(0, 7, safe(comp["tagline"]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 5, safe(comp.get("description", "")))
    pdf.ln(2)
    for section in ["capabilities", "features"]:
        items = comp.get(section, [])
        if items:
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(0, 7, section.title(), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Helvetica", "", 9)
            for item in items:
                if isinstance(item, dict):
                    pdf.cell(0, 5, safe("  " + item.get("id", "") + ": " + item.get("name", item.get("feature", "")) + " -- " + item.get("description", "")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                else:
                    pdf.cell(0, 5, safe("  - " + str(item)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, safe("Pricing: " + comp.get("pricing", "")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

# Revenue + Moat
pdf.add_page()
for section_title, section_key in [("Revenue Model", "revenue_model"), ("Competitive Moat", "moat")]:
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, section_title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    sec = data.get(section_key, {})
    items = sec.get("streams", sec.get("barriers", []))
    for item in items:
        if isinstance(item, dict):
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(0, 7, safe(item.get("name", "")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Helvetica", "", 9)
            pdf.multi_cell(0, 5, safe("  " + item.get("description", "")))
        else:
            pdf.multi_cell(0, 5, safe("  - " + str(item)))
        pdf.ln(1)
    pdf.ln(3)

# Security
pdf.add_page()
pdf.set_font("Helvetica", "B", 18)
pdf.cell(0, 10, "Security Positioning", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
sec = data.get("security_positioning", {})
if isinstance(sec, dict):
    if sec.get("headline"):
        pdf.set_font("Helvetica", "B", 12)
        pdf.multi_cell(0, 6, safe(sec["headline"]))
        pdf.ln(3)
    pdf.set_font("Helvetica", "", 10)
    if sec.get("description"):
        pdf.multi_cell(0, 5, safe(sec["description"]))
    for c in sec.get("certifications", sec.get("certifications_path", [])):
        if isinstance(c, dict):
            pdf.ln(1)
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(0, 5, safe(c.get("standard", "")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(0, 5, safe("  " + c.get("purpose", c.get("why", ""))), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

# Investment Thesis
pdf.add_page()
pdf.set_font("Helvetica", "B", 18)
pdf.cell(0, 10, "Investment Thesis", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
pdf.set_font("Helvetica", "", 10)
thesis = data.get("investment_thesis", "")
if isinstance(thesis, dict):
    for v in thesis.values():
        pdf.multi_cell(0, 5, safe(str(v)))
        pdf.ln(2)
elif isinstance(thesis, list):
    for t in thesis:
        pdf.multi_cell(0, 5, safe(str(t)))
        pdf.ln(2)
else:
    pdf.multi_cell(0, 5, safe(str(thesis)))

# Timeline
pdf.ln(5)
pdf.set_font("Helvetica", "B", 18)
pdf.cell(0, 10, "Timeline", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
pdf.set_font("Helvetica", "", 10)
for m in data.get("timeline", {}).get("milestones", []):
    if isinstance(m, dict):
        pdf.cell(0, 7, safe(m.get("date", "") + "  --  " + m.get("milestone", m.get("event", ""))), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

# Diagrams
diagram_dir = "brain/BusinessArtifacts/diagrams"
diagrams = [
    ("Component Architecture", "component_diagram.png"),
    ("Consumer Orchestrates Care", "value_chain.png"),
    ("Revenue Model", "revenue_flow.png"),
    ("Discuss Care Room", "discuss_care_room.png"),
    ("Competitive Moat", "moat_diagram.png"),
    ("Compliance Flow", "compliance_flow.png"),
]
for title, fname in diagrams:
    fpath = os.path.join(diagram_dir, fname)
    if os.path.exists(fpath):
        pdf.add_page("L")
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, safe(title), align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(3)
        try:
            pdf.image(fpath, x=15, w=250)
        except Exception as e:
            pdf.cell(0, 10, safe(f"[Diagram: {fname}]"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

pdf.output("brain/BusinessArtifacts/biz_arch_investor_v2.pdf")
print(f"PDF: {pdf.page_no()} pages")
