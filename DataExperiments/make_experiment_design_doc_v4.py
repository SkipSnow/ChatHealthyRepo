"""Medicare Diagnostic Chain -- Experiment Design v2.

Rewritten against the operator's annotations on v1:
  - a new section 1 that says how the document functions
  - sections 2 and 3 left at orienting level; section 4 carries the detail
  - the deterministic attribute chain, field by field, hop by hop
  - known-before / known-after percentages per stage
  - the elimination rules stated per COLLECTION, not in the abstract
  - indication normalisation written as the inference step it is
  - the ICD section written as objective + challenge, not commentary
  - trial facts held separate from FDA facts and said so
  - decisions the operator has now taken, removed from the open list

Every field name in this document was read from the actual source file or the
loaded table. None is recalled.
"""
import os
import sys

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.patches as patches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from docx import Document  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.enum.section import WD_ORIENT  # noqa: E402
from docx.shared import Inches, Pt, RGBColor  # noqa: E402

OUT_DIR = r"C:\CHATHealthyLLC\pipeline\ArchitectureDesignAndAudit"
DOC = os.path.join(OUT_DIR, "Medicare_DiagnosticChain_ExperimentDesign_v4.docx")
FIGDIR = r"C:\CHATHealthyLLC\DataExperiments"


def h(doc, text, level):
    p = doc.add_heading(text, level=level)
    for r in p.runs:
        r.font.color.rgb = RGBColor(0x1F, 0x38, 0x64)
    return p


def table(doc, headers, rows, widths=None, size=8.5):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Light Grid Accent 1"
    for i, v in enumerate(headers):
        c = t.rows[0].cells[i]
        c.text = str(v)
        for p in c.paragraphs:
            for r in p.runs:
                r.font.bold = True
                r.font.size = Pt(size)
    for row in rows:
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = str(v)
            for p in cells[i].paragraphs:
                for r in p.runs:
                    r.font.size = Pt(size)
    if widths:
        for row in t.rows:
            for i, w in enumerate(widths):
                row.cells[i].width = Inches(w)
    doc.add_paragraph()
    return t


def mono(doc, text):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.font.name = "Consolas"
    r.font.size = Pt(8.5)
    return p


def chain_figure():
    path = os.path.join(FIGDIR, "v2_chain.png")
    fig, ax = plt.subplots(figsize=(12.6, 3.0))
    ax.set_xlim(0, 12.6); ax.set_ylim(0, 3.0); ax.axis("off")
    boxes = [("Provider\nPrscrbr_NPI", "100%", True),
             ("Prescribed drug\nBrnd_Name + Gnrc_Name", "97.89%", True),
             ("Molecule\ningredient set", "?", False),
             ("Indication\nnormalised", "?", False),
             ("ICD-10\nL3 + L4", "", False)]
    x, w, hh = 0.2, 2.25, 1.0
    cx = []
    for label, cov, solid in boxes:
        ax.add_patch(patches.FancyBboxPatch(
            (x, 1.35), w, hh, boxstyle="round,pad=0.06", linewidth=1.7,
            edgecolor="#1f3864", facecolor="#dae3f3" if solid else "#f7f7f7",
            linestyle="-" if solid else "--"))
        ax.text(x + w / 2, 1.85, label.split("\n")[0], ha="center",
                fontsize=10, color="#1f3864", fontweight="bold")
        ax.text(x + w / 2, 1.58, label.split("\n")[1], ha="center",
                fontsize=7.4, color="#404040")
        cx.append(x + w / 2); x += w + 0.35
    labels = ["T1 bridge\nRxNorm + FDA + Trials", "T2 rollup\nsum by molecule",
              "T3 normalise\nNEW inference", "T4 crosswalk\n5 routes"]
    covs = ["97.89%", "n/a", "unmeasured", "unmeasured"]
    for i in range(4):
        x0, x1 = cx[i] + w / 2, cx[i + 1] - w / 2
        ax.annotate("", xy=(x1, 1.85), xytext=(x0, 1.85),
                    arrowprops=dict(arrowstyle="-|>", linewidth=1.5, color="#1f3864"))
        ax.text((x0 + x1) / 2, 2.52, labels[i], ha="center", fontsize=7.2,
                color="#1f3864")
        ax.text((x0 + x1) / 2, 1.05, covs[i], ha="center", fontsize=8.2,
                color="#1f3864" if covs[i] not in ("unmeasured",) else "#c00000",
                fontweight="bold")
    ax.text(6.3, 0.35, "Solid = measured.   Dashed = what this experiment establishes.",
            ha="center", fontsize=8.6, color="#404040")
    fig.savefig(path, dpi=200, bbox_inches="tight"); plt.close(fig)
    return path


def _read_prompt_2():
    """Read PROMPT 2 out of the accepted tool rather than transcribe it.

    Imported, not parsed: importing gets the exact object the agent is built
    with, so the appendix and the running prompt cannot disagree. If the
    import fails the document must not be written with a near-miss."""
    import sys
    sys.path.insert(0, r"C:\CHATHealthyLLC")
    sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
    from pipeline.MedicareProviderEvaluationDataPipeline import (
        fda_drug_enrichment_tool as t)
    text = getattr(t, "_PROMPT_2_INSTRUCTIONS", None)
    if not isinstance(text, str) or not text.strip():
        raise ChatHealthyException(
            "runtime_error",
            "_PROMPT_2_INSTRUCTIONS not found in fda_drug_enrichment_tool; "
            "refusing to write an appendix that claims to be verbatim")
    return text.strip()


def _template():
    """Prefer the most recently annotated document so the operator's footer
    and any later styling survive."""
    for name in ("Medicare_DiagnosticChain_ExperimentDesign_v2_anotated.docx",
                 "Medicare_DiagnosticChain_ExperimentDesign_v1_anotated.docx",
                 # v2 itself was generated from the annotated v1 and therefore
                 # already carries the operator's footer part verbatim
                 "Medicare_DiagnosticChain_ExperimentDesign_v2.docx"):
        cand = os.path.join(OUT_DIR, name)
        if os.path.exists(cand):
            return cand
    raise ChatHealthyException(
        "runtime_error", "no template found to inherit the footer from")


TEMPLATE = _template()


def open_from_template():
    """Start from the annotated v1 so the operator's footer, styles and page
    setup survive verbatim, then empty the body and write v2 into it.

    Rebuilding the footer by hand would risk a near-match; reusing the part
    guarantees the real one. The final sectPr is kept because it carries the
    page setup and the footer reference."""
    doc = Document(TEMPLATE)
    body = doc.element.body
    sect = body.find(qn("w:sectPr"))
    for child in list(body):
        if child is not sect:
            body.remove(child)
    return doc


def tables_figure():
    """The intermediary-table chain, with grain, the collapse factor, and
    INFERENCE marked on every table that uses it to produce a value."""
    path = os.path.join(FIGDIR, "v3_tables.png")
    fig, ax = plt.subplots(figsize=(13.0, 5.2))
    ax.set_xlim(0, 13.0); ax.set_ylim(0, 5.2); ax.axis("off")

    sources = [("Part D claims", "28,023,892 rows"),
               ("RxNorm", "RXNCONSO + RXNREL"),
               ("FDA labels", "93,956 non-OTC"),
               ("ClinicalTrials.gov", "609,551 studies"),
               ("ICD-10-CM / Coverage / CCSR", "CMS + AHRQ")]
    y = 4.55
    for name, sub in sources:
        ax.add_patch(patches.FancyBboxPatch(
            (0.15, y - 0.36), 2.55, 0.50, boxstyle="round,pad=0.04",
            linewidth=1.1, edgecolor="#7f7f7f", facecolor="#ffffff"))
        ax.text(1.42, y - 0.03, name, ha="center", fontsize=8.4, color="#1f3864")
        ax.text(1.42, y - 0.24, sub, ha="center", fontsize=6.6, color="#606060")
        y -= 0.70

    # (label, grain, facecolour, y, inference?)
    mids = [("T1  cms_drug_resolution", "3,159 rows — the bridge",
             "#dae3f3", 3.95, "PARTIAL"),
            ("T2  provider_molecule", "per NPI × molecule",
             "#dae3f3", 2.95, "NONE"),
            ("T3  molecule_indication", "per molecule × indication × source",
             "#fbe5d6", 1.95, "CORE"),
            ("T4  indication_icd", "per indication × ICD code",
             "#fbe5d6", 0.95, "CORE")]
    for label, sub, colour, yy, inf in mids:
        ax.add_patch(patches.FancyBboxPatch(
            (3.35, yy), 3.45, 0.76, boxstyle="round,pad=0.05",
            linewidth=1.7, edgecolor="#1f3864", facecolor=colour))
        ax.text(5.07, yy + 0.50, label, ha="center", fontsize=9.2,
                color="#1f3864", fontweight="bold")
        ax.text(5.07, yy + 0.21, sub, ha="center", fontsize=7.2, color="#404040")
        tag = {"NONE": ("no inference", "#2e7d32"),
               "PARTIAL": ("inference: S4 only", "#b36b00"),
               "CORE": ("INFERENCE REQUIRED", "#c00000")}[inf]
        ax.add_patch(patches.FancyBboxPatch(
            (6.95, yy + 0.16), 1.65, 0.44, boxstyle="round,pad=0.03",
            linewidth=1.1, edgecolor=tag[1], facecolor="#ffffff"))
        ax.text(7.77, yy + 0.38, tag[0], ha="center", fontsize=6.8,
                color=tag[1], fontweight="bold")

    ax.add_patch(patches.FancyBboxPatch(
        (9.15, 2.05), 3.6, 1.55, boxstyle="round,pad=0.06",
        linewidth=2.0, edgecolor="#1f3864", facecolor="#fff2cc"))
    ax.text(10.95, 3.22, "T5  the delivered data set", ha="center",
            fontsize=9.4, color="#1f3864", fontweight="bold")
    ax.text(10.95, 2.93, "provider → molecule →", ha="center", fontsize=8.4,
            color="#1f3864")
    ax.text(10.95, 2.70, "indication → ICD-10 L3 + L4", ha="center",
            fontsize=8.4, color="#1f3864")
    ax.text(10.95, 2.34, "carries the WEAKEST provenance on its path",
            ha="center", fontsize=6.8, color="#c00000", style="italic")

    for yy in (4.19, 3.49, 2.79, 2.09, 1.39):
        ax.annotate("", xy=(3.30, yy), xytext=(2.75, yy),
                    arrowprops=dict(arrowstyle="-|>", linewidth=1.0,
                                    color="#7f7f7f"))
    for yy in (4.33, 3.33, 2.33, 1.33):
        ax.annotate("", xy=(9.10, 2.85), xytext=(8.65, yy),
                    arrowprops=dict(arrowstyle="-|>", linewidth=1.1,
                                    color="#1f3864", alpha=0.6))
    ax.text(5.07, 4.92,
            "28,023,892 claims collapse to 3,159 pairs — resolution runs ONCE PER PAIR, not once per claim",
            ha="center", fontsize=8.8, color="#c00000", fontweight="bold")
    ax.text(6.5, 0.30,
            "Blue = produced by deterministic join or arithmetic.    "
            "Orange = cannot be produced without inference.    "
            "Every inferred value is stamped provenance='inference'.",
            ha="center", fontsize=7.6, color="#404040")
    fig.savefig(path, dpi=200, bbox_inches="tight"); plt.close(fig)
    return path


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    doc = open_from_template()
    for s in doc.sections:
        s.left_margin = s.right_margin = Inches(0.7)

    doc.add_heading("Medicare Diagnostic Chain — Experiment Design", 0)
    doc.add_paragraph(
        "Establishing the business rules that carry a prescribing provider to "
        "a molecule, a molecule to a normalised indication, and an indication "
        "to ICD-10 at the 3- and 4-character levels.")
    r = doc.add_paragraph("Version 4 · 2026-10-30 · local experiment").runs[0]
    r.italic = True

    # =================================================================== §1
    h(doc, "1. How this document functions", 1)
    doc.add_paragraph(
        "Read sections 2 and 3 for orientation. Section 4 is the specification.")
    table(doc,
          ["Section", "What it is for", "Level"],
          [["1", "This map", "—"],
           ["2", "Scope: which states, how big, why those two",
            "orienting"],
           ["3", "The four links named, and which are already settled",
            "orienting"],
           ["4", "THE SPECIFICATION. Every table, every field, every hop, "
            "the five intermediary tables, the deterministic attribute chain, the known-before / known-after figure for each stage, and an explicit INFERENCE CALLOUT on every table that needs inference to produce a value", "normative"],
           ["5", "How each link is measured and falsified", "normative"],
           ["6", "What the experiment delivers", "normative"],
           ["7", "Hazards already proven, so they are not repeated", "reference"],
           ["8", "Decisions still reserved to the operator", "reference"]],
          [0.8, 5.2, 1.1])
    doc.add_paragraph(
        "The deliverable is the rule set in section 6, not a coverage number.")

    # =================================================================== §2
    h(doc, "2. Scope", 1)
    doc.add_paragraph(
        "Delaware and Alabama, all claims, every provider. Both are small "
        "enough to inspect by hand and differ in affluence and specialist "
        "density, so a rule that only works in one is visible as such.")
    table(doc,
          ["", "Delaware", "Alabama"],
          [["Prescribing providers", "3,601", "15,158"],
           ["Claim rows", "86,621", "467,696"],
           ["Distinct (Brnd_Name, Gnrc_Name) pairs", "1,283", "1,834"],
           ["Distinct prescriber specialties", "72", "89"],
           ["Total claims", "4,469,302", "29,065,240"],
           ["Total drug cost", "$734,247,072", "$4,397,971,083"],
           ["Cost per claim", "$164.28", "$151.31"]],
          [2.9, 2.1, 2.1])
    doc.add_paragraph(
        "Combined: 554,317 rows (1.98% of the national file), 18,759 "
        "providers, 1,910 distinct pairs — 60.5% of the 3,159 national pairs. "
        "Pair coverage matters more than row count, because pairs are the "
        "unit of resolution.")
    doc.add_paragraph(
        "The states are reported separately at every link and never averaged.")

    # =================================================================== §3
    h(doc, "3. The four links", 1)
    doc.add_picture(chain_figure(), width=Inches(7.1))
    doc.add_paragraph()
    table(doc,
          ["Link", "Settled?", "Claim coverage", "Where specified"],
          [["Provider → prescribed drug", "yes, authoritative", "100%", "§4.3"],
           ["Prescribed drug → molecule", "yes, measured", "97.89%", "§4.4"],
           ["Molecule → indication", "no", "unmeasured", "§4.6"],
           ["Indication → ICD-10 L3/L4", "no", "unmeasured", "§4.7"]],
          [2.6, 1.6, 1.5, 1.4])

    # =================================================================== §4
    h(doc, "4. Specification", 1)

    h(doc, "4.1 The intermediary tables", 2)
    doc.add_paragraph(
        "The chain is not built by joining claims to molecules. It is built by "
        "materialising five tables, each with one grain and one job. The "
        "reason is arithmetic: 28,023,892 claim rows collapse to 3,159 "
        "distinct (Brnd_Name, Gnrc_Name) pairs. Resolution therefore runs ONCE "
        "PER PAIR and is reused by every claim — the bridge is 1/8,870th the "
        "size of the claim file. A design that resolves per claim does the "
        "same work 8,870 times and makes the national run impossible.")
    doc.add_picture(tables_figure(), width=Inches(7.2))
    doc.add_paragraph()
    table(doc,
          ["Table", "Grain", "Rows", "Built from", "Inference"],
          [["T1 cms_drug_resolution", "one per (Brnd_Name, Gnrc_Name)",
            "3,159 national / 1,910 DE+AL", "Part D + RxNorm + FDA + Trials",
            "PARTIAL — stage S4 only"],
           ["T2 provider_molecule", "one per (Prscrbr_NPI, molecule_id)",
            "to be measured", "Part D claims ⋈ T1",
            "NONE — arithmetic only"],
           ["T3 molecule_indication", "one per (molecule_id, indication_id, source)",
            "to be measured", "FDA label prose + Trials conditions",
            "REQUIRED — cannot be built without it"],
           ["T4 indication_icd", "one per (indication_id, icd10_code)",
            "to be measured", "Coverage + ICD-10-CM + CCSR + MeSH",
            "REQUIRED for the residual routes"],
           ["T5 delivered", "a decision, see §4.8", "to be measured",
            "T2 ⋈ T3 ⋈ T4", "inherits the WEAKEST on its path"]],
          [1.8, 2.0, 1.4, 2.0, 1.9], size=8)

    h(doc, "4.1.1 T1 — the CMS-name to molecule bridge", 3)
    doc.add_paragraph(
        "This is the table the 97.89% belongs to, and it is NOT the molecule "
        "table. It is the abstraction layer that lets every claim be expressed "
        "in molecule terms. Three sources jointly: RxNorm supplies ingredient "
        "identity and the salt fold, the FDA label supplies the ingredient "
        "set, ClinicalTrials.gov supplies the alias bridge for brands the "
        "other two miss.")
    table(doc,
          ["Column", "Meaning", "How produced"],
          [["brnd_name, gnrc_name", "the CMS pair verbatim; the natural key",
            "copied"],
           ["molecule_id", "the resolved molecule; NULL when unresolved, never "
            "defaulted", "deterministic for S1–S3, S5"],
           ["resolution_stage", "S1 brand_exact | S2 brand_normalized | "
            "S3 ingredient_set | S4 prefix_extended | S5 trials_bridge",
            "recorded, not derived"],
           ["provenance", "adopted_vocabulary | joined_coded_source | inference",
            "S4 stamps inference; all others stamp a join"],
           ["ingredient_set", "the normalised ingredient multiset",
            "deterministic; makes the match inspectable"],
           ["n_labels", "how many FDA labels carry this set. Many is correct — "
            "it means many manufacturers", "count"],
           ["residual_reason", "not_a_drug | vaccine_biologic | cms_truncated | "
            "unknown_to_vocabularies", "classification rule"],
           ["src_rows, tot_clms, tot_drug_cst", "the volume behind the pair",
            "summed from Part D"]],
          [2.0, 4.0, 2.4], size=8)
    doc.add_paragraph(
        "INFERENCE CALLOUT: stage S4, unique prefix extension, is the only "
        "inferred value in T1. It completes a truncated CMS token against "
        "RxNorm and accepts the result only when exactly one "
        "single-ingredient name matches. It contributed +0.41 of claim "
        "coverage and it is also the stage that produced the "
        "doxorubicin / paclitaxel defect in §7. Every S4 row must therefore be "
        "stamped provenance='inference' and must be separately reportable, so "
        "the delivered set can be measured with S4 included and excluded.")

    h(doc, "4.1.2 T2 — provider to molecule", 3)
    doc.add_paragraph(
        "The table that turns claims into clinical behaviour. Claims joined to "
        "T1 and summed by molecule — which is where the collapse pays off: "
        "several CMS pairs resolve to one molecule, so a provider's true "
        "volume is only visible after the bridge is applied. Seven Klor-Con "
        "rows are one molecule.")
    table(doc,
          ["Column", "Meaning", "How produced"],
          [["prscrbr_npi", "the provider", "copied; joins the Provider collection"],
           ["state", "DE or AL, retained so they are never averaged", "copied"],
           ["nucc_codes", "the specialty of record",
            "joined from the Provider collection by NPI, NOT from Prscrbr_Type"],
           ["molecule_id", "from T1", "join"],
           ["tot_clms, tot_day_suply, tot_drug_cst",
            "summed across every CMS pair that resolved to this molecule", "SUM"],
           ["tot_benes", "summed; explicit NULL where CMS suppressed",
            "SUM with suppression honoured"],
           ["n_source_pairs", "how many CMS names rolled into this molecule",
            "COUNT — makes the collapse auditable"],
           ["unresolved_clms", "the provider's claim volume that did NOT resolve",
            "SUM over T1 rows with molecule_id IS NULL"]],
          [2.0, 4.0, 2.4], size=8)
    doc.add_paragraph(
        "INFERENCE CALLOUT: none. T2 is joins and sums only. If any value in "
        "T2 ever requires inference, something upstream has been mis-specified.")
    doc.add_paragraph(
        "The unresolved_clms column is deliberate. A provider whose "
        "prescribing is 60% unresolved must not look identical to one at 99%, "
        "and a national average hides exactly that.")

    # -------------------------------------------------------------- 4.1.2.1
    h(doc, "4.1.2.1 Provider normalisation — the billing address is the "
           "anchor, and it is the only 1:1 key", 3)
    doc.add_paragraph(
        "T2 cannot be built by joining claims to a provider naively, and this "
        "is not a detail of convenience — it is the step that makes one "
        "provider one row. The same NPI prescribing the same drug appears on "
        "more than one Part D record. Normalising to one provider requires a "
        "canonical map of NPI to address, and only ONE of the two addresses a "
        "provider holds is capable of being that map.")
    table(doc,
          ["Address", "Cardinality with NPI", "Role"],
          [["Business (billing) address", "ONE to ONE",
            "THE ANCHOR. The file is constructed on it, because a 1:many key "
            "cannot normalise anything"],
           ["Practice address", "ONE to MANY — worst observed 356 addresses "
            "on a single NPI",
            "Shown to the END USER, never used to construct the record. "
            "Joining on it multiplies a provider by its locations"]],
          [2.2, 2.4, 3.8])
    doc.add_paragraph(
        "The normalisation is therefore asserted, not assumed. The build "
        "takes the provider spine, tests it for 1:1 against NPI, and STOPS if "
        "the test fails — because if the anchor is not 1:1 then every "
        "downstream count is wrong and no amount of later care repairs it. "
        "Measured on the two test states: 513,777 rows before the provider "
        "join and 513,777 after, a ratio of exactly 1.0000. The join added "
        "provider facts and multiplied nothing.")
    doc.add_paragraph(
        "Provider coverage of the claim file, measured rather than assumed: "
        "all 18,759 distinct prescriber NPIs in the two-state Part D file "
        "matched a provider record, and all 18,759 carry a billing address — "
        "100% on both, matched on NPI alone. No claim is lost for want of an "
        "anchor.")

    # -------------------------------------------------------------- 4.1.2.2
    h(doc, "4.1.2.2 Two state facts that are never substituted for one "
           "another", 3)
    doc.add_paragraph(
        "The chain carries two different geographic facts about a "
        "prescription, and the single most dangerous error available here is "
        "to treat either as the other. They answer different questions and "
        "they disagree for real providers.")
    table(doc,
          ["Fact", "Field", "What it means"],
          [["Where the prescription was FILLED",
            "partd.Prscrbr_State_Abrvtn",
            "a CMS fact about the claim"],
           ["Where the provider's BILLING OFFICE is",
            "the provider record's business address state",
            "a registry fact about the provider"]],
          [2.4, 2.6, 3.4])
    doc.add_paragraph(
        "Both are carried on every row as separate booleans so that neither "
        "can silently imply the other. On the two test states they diverge "
        "for 1,697 providers who filled prescriptions in state while billing "
        "from elsewhere. Any rollup that collapses the two facts into one "
        "'state' column is wrong for those providers, and the error is "
        "invisible once committed.")
    doc.add_paragraph(
        "NUCC specialty is taken from the NPI registry, NOT from the Part D "
        "file's Prscrbr_Type. The registry is the authoritative source for "
        "what a provider IS; the claim file's type field is incidental to a "
        "billing record.")

    h(doc, "4.1.3 T3 and T4 — kept separate on purpose", 3)
    doc.add_paragraph(
        "Two tables rather than one, because the two links have different "
        "authorities and different failure modes. Collapsed into one it would "
        "be impossible to say whether a wrong ICD code came from a wrong "
        "indication or a wrong crosswalk.")
    table(doc,
          ["Table", "Grain", "Carries", "Inference"],
          [["T3 molecule_indication",
            "molecule × indication × source",
            "indication_id, indication_text, source ∈ {fda_label, trial, "
            "coverage}, authority ∈ {approved, investigational}, "
            "trial_status, normalisation_rung N1–N5",
            "REQUIRED. Rungs N1–N3 are vocabulary joins; N4 is a language "
            "model and is the only way to reach prose no vocabulary matches. "
            "See §4.6.1"],
           ["T4 indication_icd",
            "indication × ICD-10 code",
            "icd10_code, level ∈ {3,4}, route R1–R5, provenance",
            "REQUIRED for the residual. R1 and R2 are coded joins; R3 and R4 "
            "are vocabulary matches; R5 is a language model. See §4.7"]],
          [1.8, 1.7, 3.4, 3.5], size=8)
    doc.add_paragraph(
        "INFERENCE CALLOUT: T3 and T4 cannot be built without inference. That "
        "is a property of the problem, not a shortcut: an approved indication "
        "arrives as prose and ICD-10 is a code set, and no published "
        "crosswalk spans the gap for every drug. What the design controls is "
        "that inference is LABELLED, SEPARABLE and MEASURED — never "
        "indistinguishable from a coded join.")

    h(doc, "4.3 Hop 1 — Part D row to T1 and T2 keys", 2)
    doc.add_paragraph("Source: the archived CMS file, 22 columns, header read "
                      "from the file itself.")
    table(doc,
          ["Part D column", "Goes to", "Rule"],
          [["Prscrbr_NPI", "T2.prscrbr_npi", "verbatim; the provider key"],
           ["Prscrbr_State_Abrvtn", "T2.state", "verbatim; DE and AL retained separately"],
           ["Brnd_Name", "T1.brnd_name", "verbatim, trimmed. Half of the natural key"],
           ["Gnrc_Name", "T1.gnrc_name", "verbatim, trimmed. Half of the natural key"],
           ["Tot_Clms", "T2.tot_clms", "summed per (NPI, molecule_id)"],
           ["Tot_Day_Suply", "T2.tot_day_suply", "summed"],
           ["Tot_Drug_Cst", "T2.tot_drug_cst", "summed"],
           ["Tot_Benes", "T2.tot_benes",
            "summed; explicit NULL, never 0, where CMS suppressed"],
           ["GE65_Bene_Sprsn_Flag", "(control)",
            "drives the NULL above; a suppressed count is not a zero count"],
           ["Prscrbr_Type", "NOT USED as specialty",
            "claim-specialty free text, not a NUCC code"],
           ["— (Provider collection)", "T2.nucc_codes",
            "taxonomies.code joined by NPI. This is the specialty of record"]],
          [2.1, 2.1, 3.4])

    h(doc, "4.4 Hop 2 — T1, the deterministic attribute chain to the molecule", 2)
    doc.add_paragraph(
        "Five stages, strictly ordered. A pair is claimed by the first stage "
        "that matches it. Each row below is the exact field-to-field hop.")
    table(doc,
          ["Stage", "Left field", "Transform", "Right field", "Gain"],
          [["S1 brand_exact", "Part D Brnd_Name", "upper()",
            "openfda.brand_name[] upper()", ""],
           ["S2 brand_normalized", "Part D Brnd_Name", "brand_join_key()",
            "openfda.brand_name[] brand_join_key()", ""],
           ["S3 ingredient_set", "Part D Gnrc_Name",
            "split on / - + ‘and’ ‘with’ → molecule_canonical() per token → "
            "RxNorm fold → multiset",
            "openfda.substance_name[] same transform → multiset",
            "set EQUALITY"],
           ["S4 prefix_extended", "unresolved token from S3",
            "unique prefix extension, single-ingredient names only",
            "RXNCONSO.STR where TTY ∈ (IN, PIN, MIN)", ""],
           ["S5 trials_bridge", "Part D Brnd_Name", "upper()",
            "armsInterventionsModule.interventions[].name upper()", ""]],
          [1.5, 1.6, 2.6, 2.4, 1.0], size=8)
    doc.add_paragraph("The two vocabulary hops inside S3 and S5, exactly:")
    mono(doc,
         "SALT FOLD (S3)\n"
         "  name            -> RXNCONSO.STR            where LAT='ENG' AND SUPPRESS='N'\n"
         "                                              AND TTY IN ('IN','PIN','MIN')\n"
         "  RXNCONSO.STR    -> RXNCONSO.RXCUI\n"
         "  RXNCONSO.RXCUI  -> RXNREL.RXCUI1           where RXNREL.RELA = 'has_form'\n"
         "  RXNREL.RXCUI2   -> the base ingredient     where RXCUI2 is TTY='IN'\n"
         "  verified:  ('142439','has_form','5489') = hydrocodone bitartrate -> hydrocodone\n"
         "  NOT 'precise_ingredient_of' -- that relates a BRAND to a precise ingredient\n"
         "\n"
         "TRIALS BRIDGE (S5)\n"
         "  Brnd_Name       -> interventions[].name\n"
         "  same intervention -> interventions[].otherNames[]\n"
         "  otherNames[]    -> RXNCONSO.STR            TTY IN ('IN','PIN','MIN')\n"
         "                  -> ingredient RXCUI\n"
         "  reverse direction also walked (Brnd_Name matches an otherName)\n"
         "  edge DISCARDED if either side is a trial-design artifact (see 4.4)")

    h(doc, "4.4.1 Known before and known after, per stage", 3)
    doc.add_paragraph("Measured over all 3,159 pairs weighted by their "
                      "1,479,628,807 claims.")
    table(doc,
          ["Stage added", "Pairs before → after", "Claims before → after", "Claim gain"],
          [["S1–S3 literal funnel (baseline)", "0 → 82.30%", "0 → 95.25%", "+95.25"],
           ["ingredient-set equality, wrong salt edge", "82.30 → 82.97%",
            "95.25 → 95.32%", "+0.07"],
           ["salt fold corrected to has_form", "82.97 → 84.08%",
            "95.32 → 97.22%", "+1.90"],
           ["S4 unique prefix extension", "84.08 → 84.49%", "97.22 → 97.63%", "+0.41"],
           ["single-ingredient restriction on prefixes", "84.49 → 84.58%",
            "97.63 → 97.68%", "+0.05"],
           ["S5 trials bridge, artifacts removed", "84.58 → 86.86%",
            "97.68 → 97.89%", "+0.21"]],
          [2.8, 1.9, 1.9, 1.1])
    doc.add_paragraph(
        "Holdout, i.e. pairs absent from the first 500,000 rows and never seen "
        "while the rules were written: 82.11% of pairs and 84.11% of claims. "
        "The gap to the development figure is the tail, and it is the honest "
        "number for unseen data.")

    h(doc, "4.5 Elimination rules, stated per collection", 2)
    doc.add_paragraph(
        "An elimination is always scoped to a named collection. The same "
        "substance may be removed from one and retained in another.")
    table(doc,
          ["Thing", "ELIMINATED from", "RETAINED in", "Why"],
          [["Trial-design artifacts: PLACEBO, SALINE, STERILE WATER, VEHICLE, "
            "SHAM, DILUENT, CONTROL, and any name containing PLACEBO or SHAM",
            "ct_alias / the T1 trials bridge vocabulary, on BOTH sides of "
            "every alias edge",
            "nothing else; they are not removed from any prescription table",
            "A placebo arm maps no molecule in either direction. 10,449 of "
            "224,688 alias edges dropped; 0 recovered pairs lost"],
           ["Dextrose as a substance",
            "nothing",
            "T1, T2, RxNorm, FDA labels, Part D claims — every collection",
            "RxNorm 4850; substance on 141 FDA labels; prescribed in 6 Part D "
            "pairs incl. 10,539 claims of Dextrose 5%-0.45% NaCl"],
           ["OTC labels",
            "fda_label and therefore the T1 ingredient-set vocabulary",
            "fda_otc_excluded, with set_id and the reason for each",
            "Part D is the prescription benefit. 168,954 excluded per corpus "
            "copy — but 116,425 of those by one unvalidated rule, see §7"],
           ["Devices and supplies: pen needles, syringes, lancets, alcohol pads",
            "the DENOMINATOR of any molecule-coverage figure",
            "T1 and T2 with residual_reason='not_a_drug', and their claim "
            "volume preserved",
            "162 pairs with no molecule and no drug label. They are not "
            "failed matches and must not depress the score"]],
          [2.2, 2.2, 2.2, 2.6], size=8)

    h(doc, "4.6 Hop 3 — molecule to NORMALISED indication", 2)
    doc.add_paragraph(
        "Objective: one indication vocabulary, shared by every source, so that "
        "a molecule's indications can be compared, counted and crosswalked. "
        "The sources do not supply this. They supply prose in several "
        "registers, and normalising it is an inference step that does not yet "
        "exist in the pipeline.")
    doc.add_paragraph("The raw inputs, by exact field:")
    table(doc,
          ["Source", "Field path", "Register"],
          [["FDA label", "indications_and_usage[]",
            "approved indication, long prose, often several conditions in one block"],
           ["FDA label", "purpose[]",
            "used where indications_and_usage is absent"],
           ["FDA label", "package_label_principal_display_panel[]",
            "last resort; recovers the indication-missing residual"],
           ["Trials", "protocolSection.conditionsModule.conditions[]",
            "condition under study, short phrases, sponsor-authored"],
           ["Trials", "derivedSection.conditionBrowseModule.meshes[].{id, term}",
            "MeSH descriptor ALREADY ASSIGNED, e.g. "
            "{id:'D000077192', term:'Adenocarcinoma of Lung'}"],
           ["Trials", "derivedSection.conditionBrowseModule.ancestors[].{id, term}",
            "the MeSH hierarchy above that descriptor"],
           ["Trials", "protocolSection.statusModule.overallStatus",
            "gate: RECRUITING, COMPLETED, TERMINATED, WITHDRAWN…"]],
          [1.3, 3.5, 3.0])
    doc.add_paragraph(
        "The MeSH descriptors are the most valuable thing in this hop. They "
        "are a controlled vocabulary already attached to the trial by the "
        "registry, which means a trial condition arrives half-normalised while "
        "a label indication arrives as raw prose.")

    h(doc, "4.6.1 The normalisation inference — two distinct jobs", 3)
    doc.add_paragraph(
        "Normalisation is not one operation. It is two, and they fail "
        "differently, so they are specified and measured separately.")
    table(doc,
          ["Job", "What it does", "Input → output", "Failure mode"],
          [["EXTRACTION / clarification",
            "Turn one prose block into the discrete conditions it names",
            "indications_and_usage[] → N candidate indication strings",
            "Splitting one condition into two, or merging two into one"],
           ["DEDUPLICATION / canonicalisation",
            "Decide that two differently-worded conditions are the same one, "
            "and elect a canonical form",
            "many candidate strings → one indication_id",
            "Collapsing two genuinely different conditions, or failing to "
            "collapse synonyms so one molecule appears to treat five things "
            "when it treats one"]],
          [1.8, 2.5, 2.3, 2.6], size=8)
    doc.add_paragraph("The normalisation ladder, most authoritative first:")
    table(doc,
          ["Rung", "Mechanism", "Provenance stamped"],
          [["N1", "The trial's own MeSH descriptor id becomes the indication_id "
            "where a MeSH descriptor exists", "joined_coded_source"],
           ["N2", "A label indication string that matches a MeSH descriptor "
            "term or a known synonym adopts that id", "adopted_vocabulary"],
           ["N3", "A label indication string that matches an ICD-10-CM code "
            "description adopts the code's own concept", "adopted_vocabulary"],
           ["N4", "Extraction and canonicalisation by language model, where no "
            "vocabulary matches", "inference"],
           ["N5", "No match from any rung: the indication is carried with its "
            "verbatim text and a null indication_id, and is counted",
            "unresolved"]],
          [0.7, 4.6, 2.4])
    doc.add_paragraph(
        "FDA and trial facts are held SEPARATE and are never merged into one "
        "assertion. T3 carries one row per (molecule_id, indication_id, "
        "source) with source ∈ {fda_label, trial, coverage} and authority ∈ "
        "{approved, investigational}. The trial rows are permitted to inform "
        "the normalisation inference — a MeSH descriptor from a trial may "
        "supply the canonical id used by a label indication — while remaining "
        "distinguishable in the delivered row. Both properties are required: "
        "the trials help the inference, and nothing presents a studied use as "
        "an approved one.")
    doc.add_paragraph(
        "The loaded trials corpus is a public data set in its own right and is "
        "expected to migrate to the front end to serve clinical-trial search; "
        "its enrichment for that purpose is out of scope here.")

    h(doc, "4.7 Hop 4 — normalised indication to ICD-10 level 3 and 4", 2)
    doc.add_paragraph(
        "Objective: for each normalised indication, the set of ICD-10-CM codes "
        "that express it, at the 3-character category level and the "
        "4-character level, each code carrying the route that produced it.")
    doc.add_paragraph("The challenges, concretely:")
    table(doc,
          ["Challenge", "The specific problem"],
          [["Code format disagreement",
            "medicare_coverage_icd10.icd10_code_id is DOTTED ('C17.0'). "
            "icd10cm_codes_2026.txt is DOTLESS ('A000'). They will not join "
            "until one form is chosen and both are normalised to it"],
           ["Level 3 versus level 4 must be derived, not assumed",
            "icd10cm_order_2026.txt carries a validity flag: 0 = category "
            "header, 1 = billable leaf. That flag is the mechanical L3/L4 "
            "distinction; the code length alone is not reliable"],
           ["The strongest coded source is procedure-keyed, not drug-keyed",
            "medicare_coverage_icd10 joins to medicare_coverage_hcpc on "
            "article_id — 1,025 articles appear in both, bridging 23,001 HCPC "
            "rows to 467,228 ICD rows. That is a Part B procedure bridge. It "
            "reaches a Part D drug only through the indication, so it informs "
            "this hop without governing it"],
           ["MeSH to ICD-10 is not in anything currently loaded",
            "Trial conditions carry MeSH ids. A MeSH→ICD-10-CM crosswalk "
            "requires UMLS MRCONSO, which is NOT loaded. The UMLS API key "
            "exists. This is a known gap and must be closed or ruled out "
            "before the hop can claim a coded route"],
           ["One indication legitimately maps to many codes",
            "Type 2 diabetes is E11 at level 3 and E11.9, E11.21, E11.65… at "
            "level 4. Coverage is therefore a set-valued question and a "
            "one-code answer is wrong by construction"]],
          [2.4, 6.0], size=8)
    doc.add_paragraph("The routes, most authoritative first:")
    table(doc,
          ["Route", "From → to", "Nature"],
          [["R1", "medicare_coverage_icd10.icd10_code_id, reached via "
            "article_id", "joined_coded_source"],
           ["R2", "MeSH descriptor id → ICD10CM code, via the UMLS crosswalk "
            "endpoint /rest/crosswalk/current/source/MSH/{descriptor}"
            "?targetSource=ICD10CM — CLOSED, see §4.7.2",
            "joined_coded_source"],
           ["R3", "indication text → icd10cm_codes_2026.txt description",
            "adopted_vocabulary"],
           ["R4", "ICD-10-CM code → DXCCSR_v2026-1.csv 'CCSR CATEGORY n' for "
            "the clinical rollup and body system", "adopted_vocabulary"],
           ["R5", "indication prose → discrete indication → code, by the "
            "BENCHMARKED pydantic-ai agent. Not an open option: the model, "
            "the prompt and the levers are already settled by a prior "
            "experiment. See §4.7.1", "inference"]],
          [0.7, 4.8, 2.2])
    doc.add_paragraph(
        "Every T4 row stamps its route. The experiment reports what share of "
        "the delivered set rests on each, because a chain that is mostly R5 is "
        "a different product from one that is mostly R1, and the difference "
        "must be visible rather than discovered.")

    # ---------------------------------------------------------------- 4.7.1
    h(doc, "4.7.1 The authority for R5 is a prior benchmark, not this "
           "experiment", 3)
    doc.add_paragraph(
        "R5 is the only link in the chain that cannot be reached by a join, "
        "and it is also the only link this experiment is FORBIDDEN to design. "
        "It was already settled empirically under story EPIC-010-F-007-S-008, "
        "whose deliverable is "
        "pipeline/ArchitectureDesignAndAudit/"
        "Medicare_IndicationMap_Benchmark_Deliverable.zip. That benchmark "
        "scored competing models against 750 real FDA labels. This experiment "
        "adopts its result wholesale. Inventing a prompt, choosing a different "
        "model, or re-tuning the levers here would discard a measured answer "
        "and substitute an unmeasured one.")
    doc.add_paragraph("What the benchmark settled, and what this experiment "
                      "therefore uses without variation:")
    table(doc,
          ["Decision", "Settled value", "Where it comes from"],
          [["Implementation",
            "FDADrugEnrichmentTool, a pydantic_ai.Agent with typed pydantic "
            "input and typed pydantic output",
            "pipeline/MedicareProviderEvaluationDataPipeline/"
            "fda_drug_enrichment_tool.py. Rule-065 statement 11 requires "
            "pydantic-ai; no facade, no raw vendor SDK, no waiver"],
           ["Prompt",
            "PROMPT 2, used VERBATIM from Appendix C of the benchmark",
            "_PROMPT_2_INSTRUCTIONS in the tool. Reproduced in Appendix C of "
            "this document for reading, not for editing"],
           ["Model", "gpt-5.6-luna, resolved from the environment rather than "
            "pinned in source",
            "CH_FDA_ENRICHMENT_MODEL. Rule-065 statement 11 forbids pinning a "
            "model in code"],
           ["Code catalog",
            "icd10_categories_top3levels.xlsx, 12,014 codes, supplied as the "
            "model's cached static prefix",
            "The same file the benchmark proved. Supplying the catalog is what "
            "makes hallucination measurable: any returned code outside it is a "
            "hallucination by definition"],
           ["Levers", "batch 25, max_transient_retries 5, backoff_base 2.0",
            "pipeline_config, as benchmarked"],
           ["Retest-on-drop", "MANDATORY, not optional",
            "Test plan §5. luna silently drops roughly 6% of batch ids; the "
            "recovery pass is what carries the result from 93% to 97.7%"]],
          [1.5, 2.6, 3.3])
    doc.add_paragraph("The benchmark's measured scores for the chosen model:")
    table(doc,
          ["Measure", "gpt-5.6-luna", "Reading"],
          [["Hallucination", "0.000",
            "no code outside the 12,014-code catalog"],
           ["Coverage", "0.982", "indications reached a code"],
           ["Legal correctness", "0.933",
            "the code is a legitimate coding of the indication"],
           ["Consistency", "0.882", "same label, same answer on repeat"],
           ["Cost", "$0.79 per 1,000", "at benchmark batch size"],
           ["Pass 3, 750 real labels", "733/750 = 97.7%",
            "AFTER retest-on-drop. 93% before it; luna dropped 45 ids and the "
            "retest recovered 39. This is the figure this experiment's R5 "
            "coverage must be measured against — not against zero, and not "
            "against a figure this experiment invents"]],
          [2.0, 1.8, 3.6])
    doc.add_paragraph(
        "PROMPT 2 performs TWO of this document's named inference points in a "
        "single call: N4, extracting discrete indications from label prose and "
        "canonicalising them, and R5, coding each to levels 3 and 4. They are "
        "not separable steps in the sanctioned tool and must not be split "
        "apart here, because the benchmark scored them together.")

    # ---------------------------------------------------------------- 4.7.2
    h(doc, "4.7.2 R2 — the gap v3 recorded is closed", 3)
    doc.add_paragraph(
        "Version 3 recorded R2 as the one named gap, on the grounds that a "
        "MeSH→ICD-10-CM crosswalk needs UMLS MRCONSO and MRCONSO is not a "
        "declared source. That reasoning was sound and its conclusion was "
        "wrong: UMLS answers the same question per descriptor through a "
        "crosswalk endpoint, from the same authority, with no 5 GB release to "
        "download. The gap is closed and the route is live.")
    table(doc,
          ["Fact", "Measured"],
          [["Descriptors the chain uses", "2,766"],
           ["Unrecoverable fetch failures", "0"],
           ["Descriptors with an ICD-10-CM mapping", "1,501 (54.3%)"],
           ["Descriptors UMLS has no mapping for",
            "1,265 — an ANSWER, not a failure; these fall through to R5"],
           ["Levels returned", "both 3-character and 4-character"],
           ["Reproducibility",
            "cached to C:/DataExperiments/Sources/umls_crosswalk_cache.json, "
            "so the measurement reruns without re-hitting a licensed service"]],
          [2.6, 4.8])

    h(doc, "4.8 T5 — the delivered grain", 2)
    doc.add_paragraph(
        "The fan-out is multiplicative: one molecule has many indications, one "
        "indication has many codes. The row count is reported at each "
        "candidate grain and the choice is not made here.")
    table(doc,
          ["Candidate grain", "Consequence"],
          [["provider × molecule, indications nested",
            "smallest; matches a provider-detail query"],
           ["provider × indication, molecules nested",
            "matches a consumer searching by their condition"],
           ["provider × ICD-10 code",
            "largest and most directly queryable; multiplies by the crosswalk"]],
          [3.0, 4.0])
    doc.add_paragraph(
        "Provenance must survive to the delivered row. If it is lost at any "
        "join the chain becomes unauditable, so every T5 row carries the "
        "weakest provenance on its path.")

    # =================================================================== §5
    h(doc, "5. Measurement", 1)
    table(doc,
          ["Measure", "Why required"],
          [["Share of CLAIM VOLUME resolved", "the business-weighted answer"],
           ["Share of DISTINCT ITEMS resolved",
            "the tail; a high volume figure hides hundreds of failed rare drugs"],
           ["PRECISION, by inspecting matches not counting them",
            "a containment bug once matched 280 labels across 145 ingredients "
            "and reported success"],
           ["The RESIDUAL enumerated item by item with its volume",
            "'few exceptions' must be readable"],
           ["Delaware and Alabama reported separately at every link",
            "a rule that only works in one state is otherwise invisible"],
           ["Rules developed on one state, tested unchanged on the other",
            "a rule tuned until it passed is not a rule"]],
          [3.0, 4.0])

    # =================================================================== §6
    h(doc, "6. Deliverable", 1)
    for item in [
        "One ordered rule list per hop, each rule with its evidence and the "
        "residual it leaves.",
        "The field-level attribute chain as built, with any divergence from §4 "
        "recorded and explained.",
        "The known-before / known-after figure for every rule added.",
        "The provenance vocabulary each table stamps, and the share of the "
        "delivered set resting on each route.",
        "The enumerated residual at every hop, with claim volume.",
        "The MeSH→ICD-10 question closed: MRCONSO loaded and R2 measured, or "
        "R2 ruled out with the reason.",
        "Delaware and Alabama side by side at every hop.",
    ]:
        doc.add_paragraph(item, style="List Bullet")

    # =================================================================== §7
    h(doc, "7. Hazards already proven", 1)
    doc.add_paragraph(
        "Each produced a confident wrong answer during the molecule work. "
        "Bringing the data local and working the details out by hand is what "
        "exposed them.")
    table(doc,
          ["Hazard", "What it did"],
          [["Containment mistaken for identity",
            "'the label mentions potassium chloride' resolved Klor-Con M20 to "
            "280 labels spanning 145 ingredients. Set equality gives 195 "
            "labels all with the identical set"],
           ["Substring matching inside a longer phrase",
            "'active ingredients:' matched inside 'INactive ingredients:' and "
            "would have keyed 244 labels to talc and lactose"],
           ["Role mistaken for identity",
            "a trials alias edge taught the bridge that dextrose is placebo, "
            "because dextrose IS the placebo arm in thousands of studies"],
           ["Wrong vocabulary edge, silently plausible",
            "the salt fold used 'precise_ingredient_of', which relates a BRAND "
            "to a precise ingredient. The correct edge is 'has_form'. "
            "Correcting it moved claims 95.25% → 97.22%"],
           ["An unvalidated exclusion dominating the corpus",
            "a purpose-section OTC rule accounts for 116,425 of 168,954 "
            "exclusions against 47,693 from FDA's own declared flag, and has "
            "never been validated. Part D carrying no OTC drugs is the test"],
           ["A denominator that does not exist",
            "population figures were quoted against 206,303 and 168,000 "
            "labels. The real prescription corpus is 93,956"],
           ["Devices counted as failed drug matches",
            "162 pairs are pen needles, syringes and alcohol pads"],
           ["Unique prefix extension crossing a clinical distinction",
            "'doxorubicin hcl peg' and 'paclitaxel protein' extend to the "
            "plain ingredient. See §8"]],
          [2.4, 5.4], size=8)

    # =================================================================== §8
    h(doc, "8. Decisions", 1)
    h(doc, "8.1 Taken", 2)
    table(doc,
          ["Decision", "Ruling"],
          [["ICD-10 granularity", "Level 3 AND level 4. This is the level the "
            "product works at"],
           ["Clinical advice", "The front end gives none, and will not"],
           ["Does a clinically distinct formulation sharing an ingredient roll "
            "up to the same molecule — pegylated liposomal doxorubicin vs "
            "doxorubicin, protein-bound vs plain paclitaxel?",
            "NO. They remain distinct. The prefix-extension rule must not "
            "collapse them, and §7's last hazard is therefore a defect to fix, "
            "not a trade-off to accept"],
           ["Trial facts and FDA facts",
            "Held separate in T3 and in the delivered row. Trials may inform "
            "the normalisation inference but never assert an approved use"],
           ["The trials corpus",
            "Becomes a public data set and migrates to the front end for "
            "clinical-trial search; enrichment for that purpose is TBD and out "
            "of scope here"]],
          [3.0, 4.5])
    h(doc, "8.2 Still open", 2)
    table(doc,
          ["Question", "Why it is the operator's"],
          [["May an investigational trial condition ever be shown to a user, "
            "and if so labelled how?",
            "Presenting a studied use is a clinical claim even when labelled"],
           ["Does the purpose-section OTC rule stand, narrow, or go?",
            "It currently removes 116,425 labels on no validation"],
           ["What provenance mix is acceptable in the delivered set — how much "
            "inference is too much?",
            "Defines what the product is claiming about itself"]],
          [3.6, 3.9])
    doc.add_paragraph(
        "Noted but not core: resolution accuracy may differ between an "
        "affluent and a poor state because brand-name prescribing resolves "
        "less well than generic. The states are measured separately so the "
        "question can be answered later; it is not a hypothesis this "
        "experiment is built around.")

    # =============================================================== appendix
    sec = doc.add_section()
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = sec.page_height, sec.page_width
    sec.left_margin = sec.right_margin = Inches(0.6)

    h(doc, "Appendix A — complete field inventory", 1)
    doc.add_paragraph(
        "Every column of every source, with its destination or the reason it "
        "is unused. Read from the actual files, not recalled. A field absent "
        "from this appendix is a field the chain does not touch.")

    h(doc, "A.1 CMS Medicare Part D — by Provider and Drug (22 columns)", 2)
    table(doc,
          ["#", "Column", "Destination", "Treatment"],
          [["1", "Prscrbr_NPI", "T2.prscrbr_npi", "verbatim; provider key; joins Provider collection"],
           ["2", "Prscrbr_Last_Org_Name", "T2.prscrbr_last_org_name", "carried for display only"],
           ["3", "Prscrbr_First_Name", "T2.prscrbr_first_name", "carried for display only"],
           ["4", "Prscrbr_City", "T2.prscrbr_city", "carried; not a key"],
           ["5", "Prscrbr_State_Abrvtn", "T2.state", "DE / AL filter; never averaged across"],
           ["6", "Prscrbr_State_FIPS", "unused", "redundant with the abbreviation"],
           ["7", "Prscrbr_Type", "NOT the specialty", "claim-specialty free text; NUCC comes from the Provider collection"],
           ["8", "Prscrbr_Type_Src", "unused", "provenance of column 7, which is unused"],
           ["9", "Brnd_Name", "T1.brnd_name", "natural key half 1; S1/S2/S5 left side"],
           ["10", "Gnrc_Name", "T1.gnrc_name", "natural key half 2; S3/S4 left side"],
           ["11", "Tot_Clms", "T1.tot_clms, T2.tot_clms", "summed; the claim-volume weight for every coverage figure"],
           ["12", "Tot_30day_Fills", "T2.tot_30day_fills", "summed"],
           ["13", "Tot_Day_Suply", "T2.tot_day_suply", "summed"],
           ["14", "Tot_Drug_Cst", "T1.tot_drug_cst, T2.tot_drug_cst", "summed"],
           ["15", "Tot_Benes", "T2.tot_benes", "summed; explicit NULL where suppressed, never 0"],
           ["16", "GE65_Sprsn_Flag", "control", "suppression of the GE65 metric block"],
           ["17", "GE65_Tot_Clms", "T2.ge65_tot_clms", "summed; the 65-and-over mirror"],
           ["18", "GE65_Tot_30day_Fills", "T2.ge65_tot_30day_fills", "summed"],
           ["19", "GE65_Tot_Drug_Cst", "T2.ge65_tot_drug_cst", "summed"],
           ["20", "GE65_Tot_Day_Suply", "T2.ge65_tot_day_suply", "summed"],
           ["21", "GE65_Bene_Sprsn_Flag", "control", "drives the NULL in column 22"],
           ["22", "GE65_Tot_Benes", "T2.ge65_tot_benes", "summed; explicit NULL where suppressed"]],
          [0.4, 2.2, 2.4, 4.6], size=8)

    h(doc, "A.2 openFDA drug label", 2)
    doc.add_paragraph(
        "93,956 non-OTC labels. 58,282 of them (62%) carry NO openfda name "
        "block at all — the block is all-or-nothing, and product_type is "
        "absent on 100% of that group. Those labels are reachable only through "
        "the narrative fields marked below.")
    table(doc,
          ["Field path", "Used for", "Coverage of the 93,956"],
          [["set_id", "T1 / T3 label key", "100%"],
           ["openfda.brand_name[]", "S1, S2 right side", "35,674 labels"],
           ["openfda.generic_name[]", "S3 right side, fallback", "35,674 labels"],
           ["openfda.substance_name[]", "S3 right side, primary", "35,035 labels"],
           ["openfda.product_type", "OTC classification, declared", "35,674 labels; 0% of the name-less group"],
           ["indications_and_usage[]", "T3 primary indication source", "essentially all non-OTC labels"],
           ["purpose[]", "T3 fallback; ALSO the OTC rule in §7", "see §7 — 116,425 exclusions"],
           ["package_label_principal_display_panel[]", "T3 last-resort indication", "recovers the indication-missing residual"],
           ["spl_product_data_elements", "the only route into the name-less 62%", "57,996 of 58,282 = 99.5%"],
           ["description", "secondary route into the name-less 62%", "53,545 of 58,282 = 91.9%"],
           ["active_ingredient", "molecule recovery, weak", "520 of 58,282 = 0.9%"],
           ["spl_unclassified_section", "'Rx only' prescription statement", "used by the OTC classifier"],
           ["how_supplied", "'Rx only' prescription statement", "used by the OTC classifier"],
           ["effective_time, version, id", "carried; label vintage", "100%"]],
          [3.0, 3.4, 3.2], size=8)

    h(doc, "A.3 RxNorm — RXNCONSO (18 cols) and RXNREL (16 cols)", 2)
    table(doc,
          ["File.column", "Used for", "Filter applied"],
          [["RXNCONSO.RXCUI", "the ingredient identifier", "—"],
           ["RXNCONSO.STR", "the name matched against both sides", "—"],
           ["RXNCONSO.TTY", "term type: what a name IS", "IN, PIN, MIN for ingredients; BN for brands"],
           ["RXNCONSO.LAT", "language", "= 'ENG'"],
           ["RXNCONSO.SUPPRESS", "suppression", "= 'N'"],
           ["RXNCONSO.SAB, CODE, SCUI, SDUI, AUI, SAUI, LUI, SUI, STT, ISPREF, TS, SRL, CVF",
            "loaded, not used by the chain", "retained so a later rule can reach them"],
           ["RXNREL.RXCUI1", "the salt / precise form", "left side of has_form"],
           ["RXNREL.RELA", "the relationship", "= 'has_form' for the salt fold"],
           ["RXNREL.RXCUI2", "the base ingredient", "must be TTY='IN'"],
           ["RXNREL.REL, STYPE1, STYPE2, RUI, SRUI, SAB, SL, RG, DIR, SUPPRESS, CVF, RXAUI1, RXAUI2",
            "loaded, not used", "retained"]],
          [3.4, 3.2, 3.0], size=8)
    doc.add_paragraph(
        "Relationship counts measured in the loaded table: has_ingredient "
        "356,684 · tradename_of 119,208 · consists_of 117,461 · has_form "
        "18,326 · has_precise_ingredient 12,406. Of these, has_form is the "
        "only one used, and it connects a PIN-named concept to an IN-named "
        "one 3,684 times against 24 for has_precise_ingredient.")

    h(doc, "A.4 ClinicalTrials.gov API v2", 2)
    doc.add_paragraph("609,551 studies, 1,259,863 interventions, 490,096 "
                      "carrying an alias.")
    table(doc,
          ["Field path", "Used for"],
          [["protocolSection.identificationModule.nctId", "study key"],
           ["protocolSection.armsInterventionsModule.interventions[].name", "S5 left side"],
           ["protocolSection.armsInterventionsModule.interventions[].otherNames[]", "S5 alias target"],
           ["protocolSection.armsInterventionsModule.interventions[].type",
            "DRUG / BIOLOGICAL / DEVICE / OTHER … ; DRUG and BIOLOGICAL are the drug population"],
           ["protocolSection.conditionsModule.conditions[]", "T3 indication, sponsor prose"],
           ["derivedSection.conditionBrowseModule.meshes[].id", "T3 normalisation rung N1; the MeSH descriptor"],
           ["derivedSection.conditionBrowseModule.meshes[].term", "the descriptor's label"],
           ["derivedSection.conditionBrowseModule.ancestors[].{id,term}", "MeSH hierarchy, for level rollup"],
           ["protocolSection.statusModule.overallStatus", "trial-status gate on T3 admission"],
           ["derivedSection.interventionBrowseModule", "present but empty on inspected records; not relied on"]],
          [5.0, 5.4], size=8)

    h(doc, "A.5 The ICD-side data sets — which they are, and where from", 2)
    doc.add_paragraph(
        "All four are ALREADY declared sources in "
        "brain/machine_artifacts/content/pipeline_config.json and were "
        "archived to dev-pipeline-sources by run 39747592. None is new. The "
        "fifth row is the one that IS new, and is therefore the operator's.")
    table(doc,
          ["pipeline_config source_name", "Publisher and fetch", "What it is"],
          [["icd10_cm",
            "CMS — static_page_latest on cms.gov/medicare/coding-billing/"
            "icd-10-codes, link_suffix '-code-descriptions-tabular-order.zip'",
            "The official ICD-10-CM code set. Supplies the code universe and, "
            "via the order file's validity flag, the mechanical level-3 vs "
            "level-4 distinction"],
           ["medicare_coverage_icd10",
            "CMS Medicare Coverage Database — direct_url "
            "downloads.cms.gov/medicare-coverage-database/downloads/exports/"
            "all_article.zip, member all_article_csv.zip → "
            "article_x_icd10_covered.csv",
            "Which ICD-10 diagnoses each Local Coverage Article declares "
            "COVERED. 467,228 rows, 1,062 distinct article_id. The strongest "
            "coded route available (R1)"],
           ["medicare_coverage_hcpc",
            "THE SAME all_article.zip, member article_x_hcpc_code.csv",
            "Which HCPCS procedures each article covers. 23,001 rows, 1,232 "
            "article_id. Because both files are members of one CMS export, "
            "article_id joins them natively — 1,025 articles appear in both, "
            "and that is the procedure↔diagnosis bridge"],
           ["ccsr",
            "AHRQ HCUP — static_page_latest on hcup-us.ahrq.gov/toolssoftware/"
            "ccsr/dxccsr.jsp",
            "DXCCSR v2026-1. Maps each ICD-10-CM code to clinical categories "
            "and a body system; this is how level 3 rolls up clinically (R4)"],
           ["— NOT DECLARED —",
            "UMLS MRCONSO, via UTS. The rxnorm source already authenticates to "
            "UTS with api_key_env UMLS_API_KEY, so the credential reaches it, "
            "but MRCONSO is not a source in pipeline_config and is not archived",
            "The only route from a trial's MeSH descriptor to an ICD-10 code "
            "(R2). Adding it is a new declared source, which is architecture "
            "and the operator's decision, not the experiment's"]],
          [2.0, 4.2, 4.2], size=8)

    h(doc, "A.5.1 Their columns", 3)
    table(doc,
          ["File", "Columns", "Used for"],
          [["icd10cm_codes_2026.txt", "DOTLESS code + whitespace + description, "
            "e.g. 'A000    Cholera due to Vibrio cholerae 01, biovar cholerae'",
            "R3 vocabulary match; the code universe"],
           ["icd10cm_order_2026.txt", "order_no, DOTLESS code, VALIDITY FLAG "
            "(0 = category header, 1 = billable leaf), short desc, long desc",
            "the mechanical level-3 vs level-4 determination"],
           ["medicare_coverage_icd10", "article_id, article_version, "
            "icd10_code_id (DOTTED), icd10_code_version, icd10_covered_group, "
            "range, sort_order, description, last_updated, asterisk",
            "R1 coded route. 467,228 rows, 1,062 distinct article_id"],
           ["medicare_coverage_hcpc", "article_id, article_version, "
            "hcpc_code_id, hcpc_code_version, hcpc_code_group, "
            "long_description, short_description, range, last_updated",
            "the procedure side of the article bridge. 23,001 rows, 1,232 "
            "article_id; 1,025 shared with the ICD file"],
           ["DXCCSR_v2026-1.csv", "'ICD-10-CM CODE' (quoted, DOTLESS), "
            "'ICD-10-CM CODE DESCRIPTION', 'Default CCSR CATEGORY IP/OP' + "
            "descriptions, 'CCSR CATEGORY 1'..'CCSR CATEGORY 5' + "
            "descriptions, body-system column",
            "R4 clinical grouping and body system for the rollup"],
           ["UMLS crosswalk endpoint", "mesh_id, icd10_code, icd_name",
            "R2 (MeSH → ICD-10). NOT MRCONSO: the per-descriptor crosswalk "
            "endpoint answers the same question from the same authority. "
            "2,766 descriptors fetched, 0 failures, 1,501 mapped. v3 recorded "
            "this as the one named gap; it is CLOSED — see §4.7.2"]],
          [2.2, 4.4, 3.8], size=8)
    doc.add_paragraph(
        "The format mismatch is load-bearing: coverage codes are dotted, "
        "ICD-10-CM and CCSR codes are dotless. One canonical form must be "
        "chosen and every source normalised to it before any join is "
        "attempted, or the hop silently returns nothing.")

    # ============================================================ appendix C
    h(doc, "Appendix C — PROMPT 2, the sanctioned prompt for N4 and R5", 1)
    doc.add_paragraph(
        "Reproduced below VERBATIM. It is not transcribed: this document reads "
        "_PROMPT_2_INSTRUCTIONS out of "
        "pipeline/MedicareProviderEvaluationDataPipeline/"
        "fda_drug_enrichment_tool.py at the moment the document is generated, "
        "so the text here cannot drift from the prompt that actually runs. If "
        "the tool's prompt changes, this appendix changes with it and the "
        "benchmark's scores stop applying until it is re-benchmarked.")
    doc.add_paragraph(
        "This prompt is the property of the benchmark, not of this "
        "experiment. It is printed here so it can be READ, not so it can be "
        "edited.")
    prompt_text = _read_prompt_2()
    p = doc.add_paragraph()
    r = p.add_run(prompt_text)
    r.font.name = "Consolas"
    r.font.size = Pt(7.5)

    doc.save(DOC)
    _log.info("%s", "document -> %s" % DOC)


main()
