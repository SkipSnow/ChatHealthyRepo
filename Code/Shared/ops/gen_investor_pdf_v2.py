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

    fig_counter = 0

    def inline_diagram(self, fname, caption="", width=170):
        fpath = os.path.join(DIAGRAM_DIR, fname)
        if os.path.exists(fpath):
            if self.get_y() > 150:
                self.add_page()
            self.ln(3)
            x = (self.w - width) / 2
            self.image(fpath, x=x, w=width)
            InvestorPDF.fig_counter += 1
            if caption:
                self.set_font("Helvetica", "I", 8)
                self.set_text_color(100, 100, 100)
                self.cell(0, 5, safe(f"Fig. {InvestorPDF.fig_counter}: {caption}"), align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                self.set_text_color(0, 0, 0)
            self.ln(4)


pdf = InvestorPDF(orientation="P", format="letter")
pdf.set_auto_page_break(auto=True, margin=20)

# ── TITLE PAGE ──
pdf.add_page()
pdf.ln(5)
# Logo
logo_path = os.path.join(DIAGRAM_DIR, "..", "..", "BusinessArtifacts", "diagrams", "competitor_logos.png")
# ChatHealthy.ai text logo at top
pdf.set_font("Helvetica", "B", 12)
pdf.set_text_color(13, 148, 136)  # teal
pdf.cell(0, 8, "+", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
pdf.ln(5)
pdf.set_font("Helvetica", "B", 32)
pdf.set_text_color(0, 0, 0)
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
pdf.inline_diagram("value_chain.png", "The consumer orchestrates the care journey. Three components serve the decision, not a linear pipeline.", width=155)

# ── COMPONENT ARCHITECTURE ──
pdf.add_page()
pdf.section_heading("Platform Architecture")
pdf.body("ChatHealthy.ai is built on three business components. The consumer is at the center, orchestrating the relationship between all three. This is not a sequential journey — it is a decision-making environment.")

# Component diagram inline
pdf.inline_diagram("component_diagram.png", "Three components share a governance layer and data infrastructure. Each operates independently but communicates through defined interfaces.", width=175)

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

    # Find Care diagram inline
    pdf.inline_diagram("find_care_diagram.png", "Chaos in, clarity out. The AI engine indexes 8.9M providers and translates natural language into precise matches. Free forever.", width=165)

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

    # Evaluate Care mixing board inline
    pdf.inline_diagram("evaluate_care_diagram.png", "Public data flows into a mixing board. The consumer tunes the algorithm. The output is a personalized provider ranking. Patent pending.", width=170)

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
    pdf.inline_diagram("discuss_care_room.png", "Inside a Discuss Care session: humans and AI models collaborate with shared tools. A bot moderator manages the floor.", width=155)

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
pdf.inline_diagram("revenue_flow.png", "Two-sided platform: consumers pay token premiums, AI vendors contribute capital. Core services remain free.", width=150)

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
pdf.inline_diagram("moat_diagram.png", "Five concentric barriers protect the platform. Trade secrets and IP at the core.", width=145)

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
pdf.inline_diagram("compliance_flow.png", "When a licensed provider enters the room, the system pauses for mandatory disclosure. All participants must acknowledge.", width=160)

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
cl = data.get("competitive_landscape", {})
if cl:
    pdf.ln(5)
    pdf.section_heading("Competitive Landscape")
    if isinstance(cl, dict):
        if cl.get("positioning"):
            pdf.body(cl["positioning"])
        if cl.get("the_gap"):
            pdf.body(cl["the_gap"])
        if cl.get("incumbents"):
            for inc in cl["incumbents"]:
                pdf.set_font("Helvetica", "B", 10)
                pdf.cell(0, 7, safe(inc.get("name", "")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_font("Helvetica", "", 9)
                pdf.body("  " + inc.get("problem", ""))
        pdf.inline_diagram("competitor_logos.png", "The healthcare navigation market. ChatHealthy.ai is the only platform that takes zero revenue from the care chain.", width=165)
        if cl.get("our_position"):
            pdf.body(cl["our_position"])
    else:
        pdf.body(str(cl))

pdf.output("brain/BusinessArtifacts/biz_arch_investor_v2.pdf")
print(f"PDF: {pdf.page_no()} pages")
