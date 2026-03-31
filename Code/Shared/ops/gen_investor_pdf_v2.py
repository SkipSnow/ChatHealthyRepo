# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Investor PDF v3 — diagrams INLINE with text, not appended at end.

import json, os
from fpdf import FPDF
from fpdf.enums import XPos, YPos

with open("brain/machine_artifacts/document_type_json/biz_arch_investor_v2.json", "r", encoding="utf-8") as f:
    data = json.load(f)

DIAGRAM_DIR = "brain/BusinessArtifacts/diagrams"

def safe(s):
    if not isinstance(s, str): s = str(s)
    for k, v in {"\u2014":"--","\u2013":"-","\u2018":"'","\u2019":"'","\u201c":'"',"\u201d":'"',"\u2022":"-","\u2026":"...","\u00a0":" ","\u2192":"->","\u2190":"<-"}.items():
        s = s.replace(k, v)
    return s.encode("latin-1", errors="replace").decode("latin-1")


class InvestorPDF(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"ChatHealthy.ai Business Architecture  |  2026-03-31  |  Page {self.page_no()}  |  Authored by Claude Code", align="C")

    def section_heading(self, title):
        self.set_font("Helvetica", "B", 16)
        self.set_text_color(0, 0, 0)
        self.cell(0, 10, safe(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(2)

    def body(self, text):
        self.set_font("Helvetica", "", 10)
        self.set_text_color(0, 0, 0)
        self.multi_cell(0, 5, safe(text), align="J")
        self.ln(2)

    def bullet(self, text):
        self.set_font("Helvetica", "", 9)
        self.set_text_color(0, 0, 0)
        self.cell(0, 5, safe("  - " + text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def inline_diagram(self, fname, width=170):
        fpath = os.path.join(DIAGRAM_DIR, fname)
        if os.path.exists(fpath):
            # Check if enough space, otherwise new page
            if self.get_y() > 160:
                self.add_page()
            self.ln(3)
            x = (self.w - width) / 2  # center
            self.image(fpath, x=x, w=width)
            self.ln(5)


pdf = InvestorPDF(orientation="P", format="letter")
pdf.set_auto_page_break(auto=True, margin=20)

# ── TITLE PAGE ──
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
pdf.multi_cell(0, 6, safe(data.get("vision", "")), align="J")
pdf.ln(15)
pdf.set_font("Helvetica", "", 9)
pdf.cell(0, 6, "Copyright 2026 Skip Snow. All rights reserved.", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

# ── EXECUTIVE SUMMARY + VALUE CHAIN DIAGRAM ──
pdf.add_page()
pdf.section_heading("Executive Summary")
es = data.get("executive_summary", "")
text = "\n\n".join(es) if isinstance(es, list) else str(es)
for p in text.split("\n\n"):
    pdf.body(p)

# Value chain inline
pdf.inline_diagram("value_chain.png", width=160)

# ── COMPONENT ARCHITECTURE ──
pdf.add_page()
pdf.section_heading("Platform Architecture")
pdf.body("ChatHealthy.ai is built on three business components. The consumer is at the center, orchestrating the relationship between all three. This is not a sequential journey — it is a decision-making environment.")

# Component diagram inline
pdf.inline_diagram("component_diagram.png", width=175)

# ── FIND CARE ──
comp = data.get("components", {}).get("find_care", {})
if comp:
    pdf.add_page()
    pdf.section_heading(comp.get("name", "Find Care"))
    if comp.get("tagline"):
        pdf.set_font("Helvetica", "I", 12)
        pdf.cell(0, 7, safe(comp["tagline"]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(3)
    pdf.body(comp.get("description", ""))
    for cap in comp.get("capabilities", []):
        pdf.bullet(str(cap) if not isinstance(cap, dict) else cap.get("name", str(cap)))
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, safe("Pricing: " + comp.get("pricing", "")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

# ── EVALUATE CARE ──
comp = data.get("components", {}).get("evaluate_care", {})
if comp:
    pdf.add_page()
    pdf.section_heading(comp.get("name", "Evaluate Care"))
    if comp.get("tagline"):
        pdf.set_font("Helvetica", "I", 12)
        pdf.cell(0, 7, safe(comp["tagline"]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(3)
    pdf.body(comp.get("description", ""))
    for cap in comp.get("capabilities", []):
        pdf.bullet(str(cap) if not isinstance(cap, dict) else cap.get("name", str(cap)))
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, safe("Pricing: " + comp.get("pricing", "")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

# ── DISCUSS CARE + ROOM DIAGRAM ──
comp = data.get("components", {}).get("discuss_care", {})
if comp:
    pdf.add_page()
    pdf.section_heading(comp.get("name", "Discuss Care"))
    if comp.get("tagline"):
        pdf.set_font("Helvetica", "I", 12)
        pdf.cell(0, 7, safe(comp["tagline"]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(3)
    pdf.body(comp.get("description", ""))

    # Room diagram inline
    pdf.inline_diagram("discuss_care_room.png", width=160)

    # Features
    features = comp.get("features", [])
    if features:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, f"Features ({len(features)})", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 8)
        for feat in features:
            if isinstance(feat, dict):
                pdf.cell(0, 5, safe("  " + feat.get("id", "") + ": " + feat.get("name", feat.get("feature", "")) + " -- " + feat.get("description", "")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            else:
                pdf.cell(0, 5, safe("  - " + str(feat)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, safe("Pricing: " + comp.get("pricing", "")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

# ── REVENUE MODEL + DIAGRAM ──
pdf.add_page()
pdf.section_heading("Revenue Model")
rm = data.get("revenue_model", {})
for s in rm.get("streams", []):
    if isinstance(s, dict):
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, safe(s.get("name", "")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 9)
        pdf.body("  " + s.get("description", ""))
    else:
        pdf.bullet(str(s))

# Revenue diagram inline
pdf.inline_diagram("revenue_flow.png", width=155)

if rm.get("gov_001_compliance"):
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, safe(rm["gov_001_compliance"]), align="J")

# ── COMPETITIVE MOAT + DIAGRAM ──
pdf.add_page()
pdf.section_heading("Competitive Moat")
moat = data.get("moat", {})
for b in moat.get("barriers", []):
    if isinstance(b, dict):
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, safe(b.get("name", "")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 9)
        pdf.body("  " + b.get("description", ""))
    else:
        pdf.body("  - " + str(b))

# Moat diagram inline
pdf.inline_diagram("moat_diagram.png", width=150)

# ── SECURITY + COMPLIANCE DIAGRAM ──
pdf.add_page()
pdf.section_heading("Security Positioning")
sec = data.get("security_positioning", {})
if isinstance(sec, dict):
    if sec.get("headline"):
        pdf.set_font("Helvetica", "B", 12)
        pdf.multi_cell(0, 6, safe(sec["headline"]), align="J")
        pdf.ln(3)
    pdf.set_font("Helvetica", "", 10)
    if sec.get("description"):
        pdf.body(sec["description"])
    for c in sec.get("certifications", sec.get("certifications_path", [])):
        if isinstance(c, dict):
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(0, 5, safe(c.get("standard", "")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(0, 5, safe("  " + c.get("purpose", c.get("why", ""))), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1)

# Compliance diagram inline
pdf.inline_diagram("compliance_flow.png", width=165)

# ── INVESTMENT THESIS ──
pdf.add_page()
pdf.section_heading("Investment Thesis")
thesis = data.get("investment_thesis", "")
if isinstance(thesis, dict):
    for v in thesis.values():
        pdf.body(str(v))
elif isinstance(thesis, list):
    for t in thesis:
        pdf.body(str(t))
else:
    pdf.body(str(thesis))

# ── TIMELINE ──
pdf.ln(3)
pdf.section_heading("Timeline")
pdf.set_font("Helvetica", "", 10)
for m in data.get("timeline", {}).get("milestones", []):
    if isinstance(m, dict):
        pdf.cell(0, 7, safe(m.get("date", "") + "  --  " + m.get("milestone", m.get("event", ""))), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

# ── COMPETITIVE LANDSCAPE ──
cl = data.get("competitive_landscape", "")
if cl:
    pdf.ln(5)
    pdf.section_heading("Competitive Landscape")
    pdf.body(str(cl))

pdf.output("brain/BusinessArtifacts/biz_arch_investor_v2.pdf")
print(f"PDF: {pdf.page_no()} pages")
