"""Generate ARCH-001 Refactor Design PDF from consolidated JSON."""
import json
from fpdf import FPDF

JSON_PATH = r"c:\chatHealthy\findCare\brain\machine_artifacts\document_type_json\refactor_design_arch001.json"
PDF_PATH = r"c:\chatHealthy\findCare\brain\BusinessArtifacts\refactor_design_arch001.pdf"

with open(JSON_PATH, "r", encoding="utf-8") as f:
    data = json.load(f)


def safe(text):
    """Sanitize text for latin-1 encoding used by core PDF fonts."""
    if not isinstance(text, str):
        text = str(text)
    replacements = {
        '\u2014': '--', '\u2013': '-', '\u2018': "'", '\u2019': "'",
        '\u201c': '"', '\u201d': '"', '\u2022': '-', '\u2026': '...',
        '\u00a0': ' ', '\u2192': '->', '\u2190': '<-',
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    # Drop any remaining non-latin-1 chars
    return text.encode('latin-1', errors='replace').decode('latin-1')


class ARCH001PDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(100, 100, 100)
        self.cell(0, 5, "ChatHealthy.ai  |  ARCH-001 Refactor Design  |  2026-03-30", align="L")
        self.ln(6)
        self.set_draw_color(200, 200, 200)
        self.line(10, self.get_y(), self.w - 10, self.get_y())
        self.ln(3)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"Copyright 2026 Skip Snow. All rights reserved.    |    Page {self.page_no()}/{{nb}}", align="C")

    def section_title(self, title):
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(0, 0, 0)
        self.ln(4)
        self.cell(0, 8, safe(title), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(0, 102, 204)
        self.set_line_width(0.5)
        self.line(10, self.get_y(), 100, self.get_y())
        self.set_line_width(0.2)
        self.ln(3)

    def sub_title(self, title):
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(40, 40, 40)
        self.cell(0, 7, safe(title), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def body_text(self, text):
        self.set_font("Helvetica", "", 9)
        self.set_text_color(0, 0, 0)
        self.set_x(self.l_margin)
        self.multi_cell(w=self.w - self.l_margin - self.r_margin, h=5, text=safe(text))
        self.ln(1)

    def bullet(self, text):
        self.set_font("Helvetica", "", 9)
        self.set_text_color(0, 0, 0)
        self.set_x(self.l_margin)
        self.multi_cell(w=self.w - self.l_margin - self.r_margin, h=5, text="  - " + safe(text))

    def table_header(self, cols, widths):
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(230, 230, 230)
        self.set_text_color(0, 0, 0)
        for i, col in enumerate(cols):
            self.cell(widths[i], 6, safe(col), border=1, fill=True)
        self.ln()

    def table_row(self, cells, widths):
        self.set_font("Helvetica", "", 8)
        self.set_text_color(0, 0, 0)
        cells = [safe(c) for c in cells]
        max_h = 6
        # Calculate needed height
        for i, cell in enumerate(cells):
            lines = self.multi_cell(widths[i], 5, cell, dry_run=True, output="LINES")
            h = len(lines) * 5
            if h > max_h:
                max_h = h
        x_start = self.get_x()
        y_start = self.get_y()
        # Check page break
        if y_start + max_h > self.h - 20:
            self.add_page()
            y_start = self.get_y()
            x_start = self.get_x()
        for i, cell in enumerate(cells):
            self.set_xy(x_start + sum(widths[:i]), y_start)
            self.multi_cell(widths[i], 5, cell, border=1, max_line_height=5)
        self.set_xy(x_start, y_start + max_h)
        self.ln(0)


pdf = ARCH001PDF(orientation="L", format="letter")
pdf.alias_nb_pages()
pdf.set_auto_page_break(auto=True, margin=18)
pdf.add_page()

# --- TITLE PAGE ---
pdf.ln(20)
pdf.set_font("Helvetica", "B", 28)
pdf.set_text_color(0, 0, 0)
pdf.cell(0, 15, "ChatHealthy.ai", align="C", new_x="LMARGIN", new_y="NEXT")
pdf.set_font("Helvetica", "B", 20)
pdf.cell(0, 12, "ARCH-001 Refactor Design", align="C", new_x="LMARGIN", new_y="NEXT")
pdf.ln(8)
pdf.set_font("Helvetica", "", 12)
pdf.set_text_color(80, 80, 80)
pdf.cell(0, 8, "Consolidated Design Document", align="C", new_x="LMARGIN", new_y="NEXT")
pdf.cell(0, 8, "Iterations 1, 3, and 5 merged into final specification", align="C", new_x="LMARGIN", new_y="NEXT")
pdf.ln(6)
pdf.cell(0, 8, f"Date: {data['date']}", align="C", new_x="LMARGIN", new_y="NEXT")
pdf.cell(0, 8, data["collaboration"], align="C", new_x="LMARGIN", new_y="NEXT")
pdf.ln(10)
pdf.set_font("Helvetica", "B", 10)
pdf.set_text_color(0, 0, 0)
pdf.cell(0, 8, data["copyright"], align="C", new_x="LMARGIN", new_y="NEXT")

# --- EXECUTIVE SUMMARY ---
pdf.add_page()
pdf.section_title("1. Executive Summary")
pdf.body_text(data["executive_summary"])

# --- TOP-LEVEL ARCHITECTURE ---
pdf.section_title("2. Top-Level Architecture")
arch = data["top_level_architecture"]
pdf.body_text(f"Pattern: {arch['pattern']}")
pdf.body_text(f"Dependency Direction: {arch['dependency_direction']}")
pdf.sub_title("Layers")
for layer in arch["layers"]:
    pdf.bullet(layer)
pdf.ln(2)

# --- BUSINESS COMPONENTS ---
pdf.section_title("3. Business Components")

# Find Care
fc = data["business_components"]["find_care"]
pdf.sub_title(f"3.1  {fc['facade']} - {fc['description']}")
cols = ["Service Class", "File", "UAT Feature", "Responsibility"]
widths = [55, 75, 55, 90]
pdf.table_header(cols, widths)
for svc in fc["services"]:
    pdf.table_row([svc["class"], svc["file"], svc["feature"], svc["responsibility"]], widths)
pdf.ln(3)
pdf.sub_title("Facade Methods")
for m in fc["facade_methods"]:
    pdf.bullet(m)
pdf.ln(3)

# Evaluate Care Quality
ecq = data["business_components"]["evaluate_care_quality"]
pdf.sub_title(f"3.2  {ecq['facade']} - {ecq['description']}")
pdf.table_header(cols, widths)
for svc in ecq["services"]:
    pdf.table_row([svc["class"], svc["file"], svc["feature"], svc["responsibility"]], widths)
pdf.ln(3)
pdf.sub_title("Facade Methods")
for m in ecq["facade_methods"]:
    pdf.bullet(m)
pdf.ln(1)
pdf.sub_title("Cross-Domain Calls")
for c in ecq["cross_calls"]:
    pdf.bullet(c)

# --- SHARED SERVICES ---
pdf.add_page()
pdf.section_title("4. Shared Infrastructure Services")
cols = ["Service Class", "File", "UAT Feature", "Responsibility"]
widths = [50, 80, 55, 90]
pdf.table_header(cols, widths)
for svc in data["shared_infrastructure"]["services"]:
    pdf.table_row([svc["class"], svc["file"], svc["feature"], svc["responsibility"]], widths)

# --- CONSENT FLOW ---
pdf.add_page()
pdf.section_title("4.1 Consent Framework -- Full Workflow Design")
cf = data["consent_flow"]
pdf.body_text(cf["description"])
pdf.ln(2)
pdf.body_text(f"Trigger: {cf['trigger']}")
pdf.ln(2)

pdf.sub_title("Two-Tier Consent Flow")
for tier in cf["tiers"]:
    pdf.sub_title(f"Tier {tier['tier']}: {tier['name']}")
    pdf.body_text(f"Prompt: \"{tier['prompt']}\"")
    if "if_yes" in tier and isinstance(tier["if_yes"], dict):
        pdf.body_text("If YES:")
        for k, v in tier["if_yes"].items():
            pdf.bullet(f"{k}: {v}")
    if "if_no" in tier:
        if isinstance(tier["if_no"], str):
            pdf.body_text(f"If NO: {tier['if_no']}")
        else:
            pdf.body_text("If NO:")
            for k, v in tier["if_no"].items():
                pdf.bullet(f"{k}: {v}")
    pdf.ln(2)

pdf.sub_title("Data Streams")
cols = ["Stream", "Consent Required", "Treatment", "Collection"]
widths = [50, 35, 120, 50]
pdf.table_header(cols, widths)
for ds in cf["data_streams"]:
    pdf.table_row([ds["stream"], str(ds["consent_required"]), ds["treatment"], ds["collection"]], widths)
pdf.ln(3)

pdf.sub_title("ConsentService Methods")
for m in cf["methods"]:
    pdf.bullet(m)
pdf.ln(2)

pdf.sub_title("Integration Points")
for ip in cf["integration_points"]:
    pdf.bullet(ip)
pdf.ln(2)

pdf.sub_title("Regulatory Notes")
for rn in cf["regulatory_notes"]:
    pdf.bullet(rn)
pdf.ln(2)

pdf.sub_title("Lead Record Schema")
for field, ftype in cf["lead_record_schema"].items():
    pdf.bullet(f"{field}: {ftype}")

# --- HUGGINGFACE SURFACE ---
pdf.add_page()
pdf.section_title("5. HuggingFace Surface Contract")
hf = data["huggingface_surface"]
pdf.sub_title("Allowed Files")
for f_name in hf["allowed_files"]:
    pdf.bullet(f_name)
pdf.ln(2)
pdf.sub_title("Responsibilities")
for r in hf["responsibilities"]:
    pdf.bullet(r)
pdf.ln(2)
pdf.body_text(f"Adapter Class: {hf['adapter_class']}")
pdf.body_text(f"Adapter File: {hf['adapter_file']}")
ep = hf["entrypoint_final_state"]
pdf.sub_title("Entrypoint Final State")
pdf.body_text(f"main.py: {ep['main_py']}")
pdf.body_text(f"Replacement: {ep['replacement']} at {ep['location']}")
for r in ep["responsibility"]:
    pdf.bullet(r)

# --- FACADE DESIGN ---
pdf.section_title("6. Facade Design")
for facade_name, facade in data["facade_design"].items():
    pdf.sub_title(f"{facade_name}  ({facade['file']})")
    pdf.body_text("Methods:")
    for m in facade["methods"]:
        pdf.bullet(m)
    pdf.body_text(f"Delegates to: {', '.join(facade['delegates_to'])}")
    if "cross_calls" in facade:
        pdf.body_text(f"Cross-calls: {', '.join(facade['cross_calls'])}")
    pdf.ln(2)

# --- TOOL REGISTRY ---
pdf.add_page()
pdf.section_title("7. Tool Registry (ToolRouter)")
tr = data["tool_registry"]
pdf.body_text(f"Class: {tr['class']}  |  File: {tr['file']}  |  Pattern: {tr['pattern']}")
pdf.ln(2)
cols = ["Tool Name", "Pydantic Model", "Handler"]
widths = [65, 60, 100]
pdf.table_header(cols, widths)
for tool, info in tr["dispatch_map"].items():
    pdf.table_row([tool, info["model"], info["handler"]], widths)
pdf.ln(3)
pdf.sub_title("Enforcement Rules")
for e in tr["enforcement"]:
    pdf.bullet(e)

# --- REPO STRUCTURE ---
pdf.add_page()
pdf.section_title("8. Repository File Tree")

def render_tree(obj, prefix="", depth=0):
    if isinstance(obj, dict):
        for key, val in obj.items():
            indent = "  " * depth
            if isinstance(val, str):
                pdf.set_font("Courier", "", 8)
                pdf.cell(0, 4, safe(f"{indent}{key}: {val}"), new_x="LMARGIN", new_y="NEXT")
            elif isinstance(val, list):
                pdf.set_font("Courier", "B", 8)
                pdf.cell(0, 4, safe(f"{indent}{key}/"), new_x="LMARGIN", new_y="NEXT")
                for item in val:
                    pdf.set_font("Courier", "", 8)
                    pdf.cell(0, 4, safe(f"{'  ' * (depth+1)}{item}"), new_x="LMARGIN", new_y="NEXT")
            else:
                pdf.set_font("Courier", "B", 8)
                pdf.cell(0, 4, safe(f"{indent}{key}/"), new_x="LMARGIN", new_y="NEXT")
                if isinstance(val, dict):
                    render_tree(val, prefix, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            pdf.set_font("Courier", "", 8)
            indent = "  " * depth
            pdf.cell(0, 4, safe(f"{indent}{item}"), new_x="LMARGIN", new_y="NEXT")

render_tree(data["repo_structure"])

# --- MIGRATION PLAN ---
pdf.add_page()
pdf.section_title("9. Migration Plan (7 Phases)")
mp = data["migration_plan"]
pdf.body_text(f"Strategy: {mp['strategy']}")
pdf.ln(2)

cols = ["Phase", "Name", "Build", "Risk", "Key Changes"]
widths = [18, 55, 20, 30, 152]
pdf.table_header(cols, widths)
for phase in mp["phases"]:
    changes_text = "; ".join(phase["changes"][:3])
    if len(phase["changes"]) > 3:
        changes_text += f" (+{len(phase['changes'])-3} more)"
    pdf.table_row([
        str(phase["phase"]),
        phase["name"],
        phase.get("build", ""),
        phase["risk"],
        changes_text
    ], widths)

pdf.ln(3)
pdf.sub_title("Safety Mechanisms")
for s in mp["safety_mechanisms"]:
    pdf.bullet(s)

# --- TESTING STRATEGY ---
pdf.add_page()
pdf.section_title("10. Testing Strategy")
ts = data["testing_strategy"]
pdf.sub_title("Test Structure")
for folder, tests in ts["structure"].items():
    pdf.set_font("Courier", "B", 9)
    pdf.cell(0, 5, safe(folder), new_x="LMARGIN", new_y="NEXT")
    for t in tests:
        pdf.set_font("Courier", "", 8)
        pdf.cell(0, 4, safe(f"    {t}"), new_x="LMARGIN", new_y="NEXT")
pdf.ln(3)

pdf.sub_title("Approach")
for key, val in ts["approach"].items():
    if isinstance(val, list):
        pdf.body_text(f"{key}:")
        for item in val:
            pdf.bullet(item)
    else:
        pdf.body_text(f"{key}: {val}")

# --- PYDANTIC STRATEGY ---
pdf.section_title("11. Pydantic Strategy")
ps = data["pydantic_strategy"]
pdf.body_text(f"Scope: {ps['scope']}")
pdf.body_text(f"Location: {ps['location']}")
pdf.sub_title("Models")
for m in ps["models"]:
    pdf.bullet(m)
pdf.ln(2)
pdf.sub_title("Enforcement")
for e in ps["enforcement"]:
    pdf.bullet(e)

# --- PERPLEXITY AUDIT RESPONSE ---
pdf.add_page()
pdf.section_title("12. Perplexity Audit Response (All 18 Findings)")
par = data["perplexity_audit_response"]
pdf.body_text(f"Audit Source: {par['audit_source']}")
pdf.body_text(f"Total Findings: {par['total_findings']}")
summary = par["summary"]
pdf.body_text(f"Resolved: {summary['resolved_in_sprint']}  |  Deferred: {summary['deferred_to_next_sprint']}  |  Acknowledged: {summary['acknowledged_for_hardening']}  |  Strengths: {summary['confirmed_strengths']}")
pdf.ln(3)

cols = ["ID", "Title", "Status", "Response"]
widths = [18, 55, 30, 172]
pdf.table_header(cols, widths)
for finding in par["findings"]:
    pdf.table_row([
        finding["id"],
        finding["title"],
        finding["status"],
        finding["response"]
    ], widths)

# --- SPRINT SCHEDULE ---
pdf.add_page()
pdf.section_title("13. Sprint Schedule (Tue-Sun, B-187 through B-201)")
ss = data["sprint_schedule"]
pdf.body_text(f"Sprint Start: {ss['sprint_start']}")
pdf.body_text(f"Sprint Goal: {ss['sprint_goal']}")
pdf.ln(2)

cols = ["Day", "Theme", "Build", "Task", "Risk", "Deploy"]
widths = [42, 52, 18, 80, 28, 18]
pdf.table_header(cols, widths)
for day in ss["schedule"]:
    for build in day["builds"]:
        pdf.table_row([
            day["day"],
            day["theme"],
            build["id"],
            build["task"],
            build["risk"],
            build["deploy"]
        ], widths)

pdf.ln(3)
pdf.sub_title("Enhancements Included")
for enh in ss["enhancements_included"]:
    pdf.bullet(f"{enh['id']}: {enh['title']} ({enh['day']})")

pdf.ln(2)
pdf.sub_title("Deferred to Next Sprint")
for d in ss["deferred_to_next_sprint"]:
    pdf.bullet(d)

# --- ARCHITECTURAL DECISIONS ---
pdf.add_page()
pdf.section_title("14. Architectural Decisions")
for dec in data["architectural_decisions"]:
    pdf.sub_title(dec["decision"])
    pdf.body_text(dec["rationale"])
    pdf.ln(1)

# --- GOVERNANCE ALIGNMENT ---
pdf.section_title("15. Governance Alignment")
cols = ["Policy", "Status"]
widths = [30, 200]
pdf.table_header(cols, widths)
for policy, status in data["governance_alignment"].items():
    pdf.table_row([policy, status], widths)

# --- SIGN-OFF ---
pdf.add_page()
pdf.section_title("16. Sign-Off")
for agent, info in data["sign_off"].items():
    pdf.sub_title(f"{info['role']}: {info['model']}")
    pdf.body_text(f"Status: {info['status']}")
    pdf.body_text(f"Notes: {info['notes']}")
    pdf.ln(3)

pdf.ln(10)
pdf.set_font("Helvetica", "I", 10)
pdf.set_text_color(100, 100, 100)
pdf.cell(0, 8, "End of Document", align="C")

# Save
pdf.output(PDF_PATH)
print(f"PDF generated: {PDF_PATH}")
print(f"Pages: {pdf.page_no()}")
