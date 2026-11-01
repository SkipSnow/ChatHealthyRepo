"""Generate the Medicare Diagnostic Chain experiment design document.

Writes a .docx design document plus the chain figure it embeds. Every number
in the document is one measured in the local Postgres experiment database or
streamed from the archived sources; nothing is estimated.
"""
import sys
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as patches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from docx import Document  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH  # noqa: E402
from docx.shared import Inches, Pt, RGBColor  # noqa: E402

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

OUT_DIR = r"C:\CHATHealthyLLC\pipeline\ArchitectureDesignAndAudit"
FIG = r"C:\CHATHealthyLLC\DataExperiments\chain_figure.png"
DOC = os.path.join(OUT_DIR, "Medicare_DiagnosticChain_ExperimentDesign_v1.docx")

LINKS = [
    ("Provider", "Prescribed drug", "Part D claim", "authoritative", "100%"),
    ("Prescribed drug", "Molecule", "RxNorm + FDA label + Trials", "measured", "97.89%"),
    ("Molecule", "Indication", "FDA label prose + Trial conditions", "unmeasured", "?"),
    ("Indication", "ICD-10 L3 / L4", "ICD-10-CM + Coverage + CCSR", "unmeasured", "?"),
]


def figure():
    fig, ax = plt.subplots(figsize=(11.5, 3.3))
    ax.set_xlim(0, 11.5)
    ax.set_ylim(0, 3.3)
    ax.axis("off")
    boxes = ["Provider\n(NPI)", "Prescribed\ndrug", "Molecule",
             "Indication", "ICD-10\nL3 / L4"]
    x = 0.25
    w, h = 1.75, 0.95
    centers = []
    for i, label in enumerate(boxes):
        solid = i <= 2
        ax.add_patch(patches.FancyBboxPatch(
            (x, 1.6), w, h, boxstyle="round,pad=0.06",
            linewidth=1.6, edgecolor="#1f3864",
            facecolor="#dae3f3" if solid else "#f2f2f2",
            linestyle="-" if solid else "--"))
        ax.text(x + w / 2, 1.6 + h / 2, label, ha="center", va="center",
                fontsize=10.5, color="#1f3864")
        centers.append(x + w / 2)
        x += w + 0.65
    for i in range(4):
        x0 = centers[i] + w / 2
        x1 = centers[i + 1] - w / 2
        ax.annotate("", xy=(x1, 2.07), xytext=(x0, 2.07),
                    arrowprops=dict(arrowstyle="-|>", linewidth=1.5,
                                    color="#1f3864"))
        src, _, how, status, cov = LINKS[i]
        ax.text((x0 + x1) / 2, 2.30, cov, ha="center", fontsize=10,
                color="#1f3864" if status != "unmeasured" else "#c00000",
                fontweight="bold")
        ax.text((x0 + x1) / 2, 1.30, LINKS[i][2], ha="center", fontsize=7.6,
                color="#404040", wrap=True)
        ax.text((x0 + x1) / 2, 1.08, status, ha="center", fontsize=7.6,
                color="#1f3864" if status != "unmeasured" else "#c00000",
                style="italic")
    ax.text(5.75, 0.45,
            "Solid = established by measurement.   Dashed = the subject of this experiment.",
            ha="center", fontsize=9, color="#404040")
    fig.savefig(FIG, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return FIG


def tables_figure():
    """The intermediary-table chain, with grain and the collapse factor."""
    path = r"C:\CHATHealthyLLC\DataExperiments\tables_figure.png"
    fig, ax = plt.subplots(figsize=(11.5, 4.6))
    ax.set_xlim(0, 11.5)
    ax.set_ylim(0, 4.6)
    ax.axis("off")

    sources = [("Part D claims", "28,023,892 rows"),
               ("RxNorm", "RXNCONSO + RXNREL"),
               ("FDA labels", "93,956 non-OTC"),
               ("ClinicalTrials.gov", "609,551 studies")]
    y = 3.95
    for name, sub in sources:
        ax.add_patch(patches.FancyBboxPatch(
            (0.2, y - 0.38), 2.25, 0.52, boxstyle="round,pad=0.04",
            linewidth=1.2, edgecolor="#7f7f7f", facecolor="#ffffff"))
        ax.text(1.32, y - 0.04, name, ha="center", fontsize=9, color="#1f3864")
        ax.text(1.32, y - 0.26, sub, ha="center", fontsize=7, color="#606060")
        y -= 0.72

    mids = [("T1  cms_drug_resolution", "3,159 rows\nthe bridge", "#dae3f3", 3.3),
            ("T2  provider_molecule", "per NPI x molecule", "#dae3f3", 2.25),
            ("T3  molecule_indication", "per molecule x indication", "#f2f2f2", 1.2),
            ("T4  indication_icd", "per indication x ICD code", "#f2f2f2", 0.15)]
    for label, sub, colour, yy in mids:
        ax.add_patch(patches.FancyBboxPatch(
            (3.15, yy), 3.0, 0.78, boxstyle="round,pad=0.05",
            linewidth=1.6, edgecolor="#1f3864", facecolor=colour,
            linestyle="-" if colour == "#dae3f3" else "--"))
        ax.text(4.65, yy + 0.52, label, ha="center", fontsize=9.5,
                color="#1f3864", fontweight="bold")
        ax.text(4.65, yy + 0.21, sub, ha="center", fontsize=7.6, color="#404040")

    ax.add_patch(patches.FancyBboxPatch(
        (7.0, 1.35), 2.9, 1.55, boxstyle="round,pad=0.06",
        linewidth=2.0, edgecolor="#1f3864", facecolor="#fff2cc"))
    ax.text(8.45, 2.52, "T5", ha="center", fontsize=11, color="#1f3864",
            fontweight="bold")
    ax.text(8.45, 2.20, "provider -> molecule", ha="center", fontsize=8.6,
            color="#1f3864")
    ax.text(8.45, 1.98, "-> indication -> ICD 3/4", ha="center", fontsize=8.6,
            color="#1f3864")
    ax.text(8.45, 1.62, "the delivered data set", ha="center", fontsize=7.6,
            color="#606060", style="italic")

    for yy in (3.69, 2.64, 1.59, 0.54):
        ax.annotate("", xy=(3.1, yy), xytext=(2.5, yy),
                    arrowprops=dict(arrowstyle="-|>", linewidth=1.2,
                                    color="#7f7f7f"))
    for yy in (3.69, 2.64, 1.59, 0.54):
        ax.annotate("", xy=(6.95, 2.1), xytext=(6.2, yy),
                    arrowprops=dict(arrowstyle="-|>", linewidth=1.2,
                                    color="#1f3864", alpha=0.65))
    ax.text(4.65, 4.28,
            "28,023,892 claims collapse to 3,159 pairs — resolution runs once per pair, not once per claim",
            ha="center", fontsize=9, color="#c00000", fontweight="bold")
    ax.text(5.75, 0.02,
            "Solid = measured.   Dashed = the subject of this experiment.",
            ha="center", fontsize=8.4, color="#404040")
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return path


def h(doc, text, level):
    p = doc.add_heading(text, level=level)
    for r in p.runs:
        r.font.color.rgb = RGBColor(0x1F, 0x38, 0x64)
    return p


def table(doc, headers, rows, widths=None):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Light Grid Accent 1"
    for i, val in enumerate(headers):
        cell = t.rows[0].cells[i]
        cell.text = str(val)
        for p in cell.paragraphs:
            for r in p.runs:
                r.font.bold = True
                r.font.size = Pt(9)
    for row in rows:
        cells = t.add_row().cells
        for i, val in enumerate(row):
            cells[i].text = str(val)
            for p in cells[i].paragraphs:
                for r in p.runs:
                    r.font.size = Pt(9)
    if widths:
        for row in t.rows:
            for i, wd in enumerate(widths):
                row.cells[i].width = Inches(wd)
    doc.add_paragraph()
    return t


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    fig = figure()
    doc = Document()
    for s in doc.sections:
        s.left_margin = s.right_margin = Inches(0.8)

    doc.add_heading("Medicare Diagnostic Chain — Experiment Design", 0)
    p = doc.add_paragraph(
        "Establishing the end-to-end business rules that map a prescribing "
        "provider to a molecule, a molecule to an indication, and an "
        "indication to ICD-10 at the 3- and 4-character levels.")
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    doc.add_paragraph("Version 1 · 2026-10-30 · local experiment, "
                      "ChatHealthy.ai").italic = True

    # ---------------------------------------------------------------- question
    h(doc, "1. The question", 1)
    doc.add_paragraph(
        "The Medicare diagnostic pipeline must answer: given an indication, "
        "which providers have demonstrated relevant clinical behaviour? That "
        "requires a chain of four links. Two are settled. Two are not, and "
        "nobody has measured them.")
    doc.add_picture(fig, width=Inches(6.9))
    doc.add_paragraph()
    doc.add_paragraph(
        "The purpose of this experiment is NOT to raise a coverage number. It "
        "is to discover and document the business rules each link requires, "
        "with the evidence for each rule and the residual it leaves. A rule "
        "that cannot be stated and defended is not a rule; it is a habit.")

    h(doc, "1.1 What is already established", 2)
    table(doc,
          ["Link", "Sources", "Status", "Claim coverage"],
          [[f"{a} → {b}", c, d, e] for a, b, c, d, e in LINKS],
          [1.9, 2.3, 1.2, 1.2])
    doc.add_paragraph(
        "The drug→molecule figure of 97.89% is measured over all 3,159 "
        "distinct national (brand, generic) pairs weighted by their 1,479,628,807 "
        "claims. It is not a sample and not an extrapolation.")

    # ------------------------------------------------------------------- scope
    h(doc, "2. Scope — Delaware and Alabama", 1)
    doc.add_paragraph(
        "Both states are small enough that every provider can be inspected by "
        "hand. They were chosen for CONTRAST rather than size: Delaware is "
        "affluent and specialist-dense; Alabama is poorer and more "
        "primary-care weighted. The chain must hold for both, and if it does "
        "not, the difference is itself a finding.")
    table(doc,
          ["", "Delaware", "Alabama"],
          [["Prescribing providers", "3,601", "15,158"],
           ["Claim rows", "86,621", "467,696"],
           ["Distinct (brand, generic) pairs", "1,283", "1,834"],
           ["Distinct prescriber specialties", "72", "89"],
           ["Total claims", "4,469,302", "29,065,240"],
           ["Total drug cost", "$734,247,072", "$4,397,971,083"],
           ["Cost per claim", "$164.28", "$151.31"]],
          [2.6, 2.1, 2.1])
    doc.add_paragraph(
        "Combined: 554,317 claim rows (1.98% of the national file), 18,759 "
        "providers, and 1,910 distinct pairs — 60.5% of the 3,159 national "
        "pairs. The pair coverage matters more than the row count, because "
        "pairs are what the chain resolves.")

    h(doc, "2.1 The hypothesis the contrast tests", 2)
    doc.add_paragraph(
        "Brand-name prescribing is the weakest link in the molecule map: "
        "brand-typed CMS names resolve 91.43% of their claims against 99.79% "
        "for generic-typed names. Brand-heavy prescribing is expected to track "
        "affluence. If so, resolution accuracy will differ between Delaware "
        "and Alabama for reasons of socioeconomics rather than data quality.")
    doc.add_paragraph(
        "That would mean the product's accuracy is not uniform across the "
        "population it serves. It is better to measure that deliberately now "
        "than to discover it in production, and it is the operator's decision "
        "what to do about it. The experiment measures it; it does not assume "
        "the direction.")

    # ------------------------------------------------------------------- links
    h(doc, "3. The links, the candidate rules, and how each is falsified", 1)

    h(doc, "3.1 Provider → prescribed drug", 2)
    doc.add_paragraph(
        "Authoritative and requires no rule. The Part D row IS the fact: a "
        "named NPI prescribed a named drug, with claim count, day supply, "
        "beneficiary count and cost. The only business rules here are "
        "suppression handling and attribution.")
    table(doc,
          ["Candidate rule", "Why", "How falsified"],
          [["A suppressed beneficiary count is carried as explicit null, never zero",
            "CMS suppresses counts below 11; zero would understate a real provider",
            "Count rows where the suppression flag is set and the value is 0 rather than null"],
           ["A provider's specialty comes from the Provider collection by NPI, not from Prscrbr_Type",
            "Prscrbr_Type is claim-specialty text, not a NUCC code",
            "Join both and report disagreement rate per state"]],
          [2.3, 2.3, 2.3])

    h(doc, "3.2 Prescribed drug → molecule", 2)
    doc.add_paragraph(
        "Established at 97.89% of claims. The rules below are already proven "
        "and are recorded here so the experiment does not rediscover them.")
    table(doc,
          ["Proven rule", "Evidence"],
          [["Match on ingredient SET EQUALITY, never on whether a label mentions a name",
            "Klor-Con M20 matched 280 labels spanning 145 ingredients under containment; "
            "set equality gives 195 labels all with the identical set"],
           ["Fold salt forms to the base ingredient through RxNorm rela='has_form'",
            "has_form connects PIN-named to IN-named concepts 3,684 times; "
            "precise_ingredient_of does so 24 times and mostly relates a BRAND to a "
            "precise ingredient. Correcting this edge moved claims 95.25% → 97.22%"],
           ["Extend a truncated CMS token only when it has exactly ONE "
            "single-ingredient completion in RxNorm",
            "CMS truncates at ~30 characters. Unique extension recovered claims to "
            "97.68%; ambiguous prefixes such as 'multivit' (200 candidates) are rejected"],
           ["ClinicalTrials.gov supplies a third bridge via intervention otherNames",
            "Recovered 72 pairs / 3,127,117 claims that neither RxNorm nor the label "
            "resolved, concentrated in post-2020 brands: Kerendia→finerenone, "
            "Ubrelvy→ubrogepant, Camzyos→mavacamten"],
           ["Eliminate trial-design artifacts from the trials source on BOTH sides of "
            "every alias edge",
            "A placebo arm maps no molecule in either direction. 'Dextrose In Water' "
            "bridged to PLACEBO. Dropping 10,449 of 224,688 edges cost 0 recovered pairs "
            "and leaves 0 placebo-named sources"],
           ["Never eliminate a substance because it is used as a placebo",
            "Dextrose is RxNorm 4850, names 141 FDA labels as a substance, and is "
            "prescribed in 6 Part D pairs including 10,539 claims of "
            "Dextrose 5%-0.45% NaCl"]],
          [3.0, 3.9])

    h(doc, "3.3 Molecule → indication  (UNMEASURED)", 2)
    doc.add_paragraph(
        "Two independent sources, and they disagree in kind. The label states "
        "what a drug is APPROVED for. A trial states what a drug is being "
        "STUDIED for. Both are indications; they are not the same claim, and "
        "conflating them would let an experimental use masquerade as an "
        "approved one.")
    table(doc,
          ["Source", "What it actually asserts", "Known limitation"],
          [["FDA label indications_and_usage",
            "The approved indication, as prose",
            "Present on essentially every non-OTC label, but prose — not coded"],
           ["FDA label purpose / display panel",
            "An indication where the main section is absent",
            "Also the field that drives the OTC exclusion; overlap must be handled"],
           ["ClinicalTrials.gov study conditions",
            "The condition under study for an intervention",
            "Investigational, not approved. Includes failed and terminated trials"],
           ["Medicare Coverage (procedure → covered diagnoses)",
            "Which diagnoses Medicare will pay a procedure for",
            "Procedure-keyed, so it reaches drugs only indirectly"]],
          [2.2, 2.4, 2.4])
    doc.add_paragraph(
        "Candidate rules to be tested, in decreasing authority:")
    table(doc,
          ["Candidate rule", "How it will be falsified"],
          [["An approved-label indication outranks a trial condition, and the "
            "provenance is recorded on every indication",
            "Count molecules where the two sources disagree; inspect a sample by hand"],
           ["A trial condition is admitted only from a completed or active trial, "
            "never a withdrawn or terminated one",
            "Measure how many indications are gained and lost by applying the filter"],
           ["A molecule with no indication from any source is reported, never "
            "defaulted to empty",
            "Enumerate the residual and confirm it is non-silent"]],
          [3.5, 3.4])

    h(doc, "3.4 Indication → ICD-10 level 3 and 4  (UNMEASURED)", 2)
    doc.add_paragraph(
        "The hardest link and the one with the most room for a confident wrong "
        "answer. An indication is prose; ICD-10-CM is a code set. The design "
        "contract already distinguishes three provenances for the crosswalk — "
        "adopted_vocabulary, joined_coded_source, inference — and the "
        "experiment must establish how much of the chain rests on each, "
        "because a result that is 90% inference is a different product from "
        "one that is 90% coded join.")
    table(doc,
          ["Route", "Nature", "Why it matters"],
          [["Medicare Coverage ICD-10 article data",
            "joined_coded_source",
            "A real coded join published by CMS. Highest authority available"],
           ["ICD-10-CM code set descriptions",
            "adopted_vocabulary",
            "Lets an indication be matched to a code's own description text"],
           ["CCSR clinical groupings",
            "adopted_vocabulary",
            "Groups ICD codes clinically, which is how level 3 rolls up"],
           ["Trial condition MeSH terms",
            "joined_coded_source",
            "Conditions are MeSH-indexed; MeSH→ICD is a published crosswalk"],
           ["Language-model mapping of indication prose to a code",
            "inference",
            "Last resort. Must be labelled as inference and must never be "
            "indistinguishable from a coded join"]],
          [2.3, 1.9, 2.8])
    doc.add_paragraph(
        "Level 3 versus level 4 is a requirement question, not an engineering "
        "one: level 3 is the category (E11, type 2 diabetes) and level 4 adds "
        "the clinical detail (E11.9, without complications). The experiment "
        "will report coverage at both and will not choose between them.")

    # ---------------------------------------------------- intermediary tables
    h(doc, "4. The intermediary tables", 1)
    doc.add_paragraph(
        "The chain is not built by joining claims to molecules. It is built by "
        "materialising a small number of intermediary tables, each with one "
        "grain and one job. The reason is arithmetic: 28,023,892 claim rows "
        "collapse to 3,159 distinct (brand, generic) pairs. Resolution is "
        "therefore performed ONCE PER PAIR and reused by every claim — the "
        "bridge is 1/8,870th the size of the claim file. A design that "
        "resolves per claim does the same work 8,870 times over and makes "
        "the national run impossible.")
    doc.add_picture(tables_figure(), width=Inches(6.9))
    doc.add_paragraph()

    h(doc, "4.1 T1 — the CMS-name to molecule bridge", 2)
    doc.add_paragraph(
        "This is the table the 97.89% belongs to, and it is NOT the molecule "
        "table. It is the abstraction layer that lets every claim be expressed "
        "in molecule terms. Built from three sources jointly: RxNorm supplies "
        "ingredient identity and salt folding, the FDA label supplies the "
        "ingredient set, ClinicalTrials.gov supplies the alias bridge for "
        "brands the other two miss.")
    table(doc,
          ["Column", "Meaning"],
          [["brnd_name, gnrc_name", "The CMS pair, verbatim. The natural key"],
           ["molecule_id", "The resolved molecule. Null when unresolved — never defaulted"],
           ["resolution_stage", "Which rule resolved it: brand_exact, brand_normalized, "
            "ingredient_set, prefix_extended, trials_bridge"],
           ["provenance", "adopted_vocabulary | joined_coded_source | inference"],
           ["n_labels", "How many FDA labels carry this ingredient set. Many is "
            "expected and correct; it means many manufacturers"],
           ["ingredient_set", "The normalised ingredient multiset, so a match is "
            "inspectable rather than asserted"],
           ["residual_reason", "Why it did not resolve: not_a_drug, vaccine_biologic, "
            "cms_truncated, unknown_to_vocabularies"],
           ["src_rows, tot_clms, tot_drug_cst", "The volume behind the pair, so any "
            "coverage figure can be claim-weighted"]],
          [2.3, 4.6])
    doc.add_paragraph(
        "Measured grain: 3,159 rows nationally; 1,910 for Delaware and Alabama "
        "combined. This table is the unit of all resolution work.")

    h(doc, "4.2 T2 — provider to molecule", 2)
    doc.add_paragraph(
        "The second intermediary, and the one that turns claims into clinical "
        "behaviour. Produced by joining the claim rows to T1 and summing by "
        "molecule, which is where the collapse pays off: several CMS pairs "
        "resolve to the same molecule — seven Klor-Con rows are one molecule — "
        "so a provider's true volume for a molecule is only visible after the "
        "bridge is applied.")
    table(doc,
          ["Column", "Meaning"],
          [["prscrbr_npi", "The provider. Joins to the Provider collection for NUCC"],
           ["state", "Retained so Delaware and Alabama are never averaged together"],
           ["molecule_id", "From T1"],
           ["tot_clms, tot_day_suply, tot_drug_cst", "Summed across every CMS pair "
            "that resolved to this molecule"],
           ["tot_benes", "Summed where present; explicit null where CMS suppressed it"],
           ["n_source_pairs", "How many distinct CMS names rolled into this molecule, "
            "so the collapse is auditable"],
           ["unresolved_clms", "The provider's claim volume that did NOT resolve, "
            "carried on the row so a provider's coverage is visible per provider"]],
          [2.3, 4.6])
    doc.add_paragraph(
        "The unresolved_clms column is deliberate. A provider whose prescribing "
        "is 60% unresolved must not look identical to one at 99%, and a "
        "national average hides exactly that.")

    h(doc, "4.3 T3 — molecule to indication, and T4 — indication to ICD-10", 2)
    doc.add_paragraph(
        "Kept as two tables rather than one, because the two links have "
        "different authorities and different failure modes, and collapsing "
        "them would make it impossible to say whether a wrong ICD code came "
        "from a wrong indication or a wrong crosswalk.")
    table(doc,
          ["Table", "Grain", "Carries"],
          [["T3 molecule_indication",
            "one row per molecule × indication × source",
            "indication text, source (label | trial | coverage), authority "
            "(approved | investigational), trial status where applicable"],
           ["T4 indication_icd",
            "one row per indication × icd10 code",
            "icd10_code, level (3 or 4), route (coverage | icd10cm_description | "
            "ccsr | mesh | llm), provenance"]],
          [1.9, 2.1, 3.2])

    h(doc, "4.4 T5 — the delivered data set", 2)
    doc.add_paragraph(
        "The join T2 → T3 → T4 produces the collection the pipeline exists to "
        "build: for a given indication or ICD code, which providers have "
        "demonstrated relevant prescribing, and at what volume. Because the "
        "fan-out is multiplicative — one molecule has many indications, one "
        "indication has many codes — the delivered grain is a decision, and "
        "the experiment will report the row count at each candidate grain "
        "rather than assume one.")
    table(doc,
          ["Candidate grain", "Consequence"],
          [["One row per provider × molecule, indications nested",
            "Smallest. Matches how the front end queries by provider"],
           ["One row per provider × indication, molecules nested",
            "Matches how a consumer searches — by their condition"],
           ["One row per provider × ICD-10 code",
            "Largest and most directly queryable, but multiplies by the crosswalk"]],
          [3.2, 3.7])
    doc.add_paragraph(
        "Every row of T5 carries the provenance it inherited, so a result "
        "resting on an inferred ICD mapping can always be told apart from one "
        "resting on a CMS coded join. That property must survive to the "
        "delivered row; if it is lost at any join, the whole chain becomes "
        "unauditable.")

    # ------------------------------------------------------------ measurement
    h(doc, "5. Measurement", 1)
    doc.add_paragraph(
        "Every link is reported the same four ways, because a single number "
        "hides the failure modes that matter.")
    table(doc,
          ["Measure", "Why it is required"],
          [["Share of CLAIM VOLUME resolved",
            "The business-weighted answer. What most prescribing actually reaches"],
           ["Share of DISTINCT ITEMS resolved",
            "The long tail. A high volume figure can hide hundreds of failed rare drugs"],
           ["PRECISION, by inspecting matches rather than counting them",
            "Resolution to the wrong answer is worse than no resolution. A "
            "containment bug once matched 280 labels across 145 ingredients and "
            "reported success"],
           ["The RESIDUAL, enumerated item by item with its volume",
            "'Few exceptions' must be readable, not asserted"]],
          [2.8, 4.1])
    doc.add_paragraph(
        "Two further controls apply to the whole experiment:")
    table(doc,
          ["Control", "Purpose"],
          [["Delaware and Alabama are measured and reported SEPARATELY at every link",
            "The affluence hypothesis in §2.1 is only visible if the states are "
            "never averaged together"],
           ["Rules are developed on one state and tested unchanged on the other",
            "A rule tuned until it passed is not a rule. Development and test "
            "must be different data"]],
          [3.4, 3.5])

    # ------------------------------------------------------------- acceptance
    h(doc, "6. What the experiment delivers", 1)
    doc.add_paragraph(
        "The deliverable is NOT a coverage percentage. It is a documented, "
        "evidenced rule set, which is the thing the production pipeline needs "
        "and does not currently have:")
    for item in [
        "One ordered rule list per link, each rule with the evidence that "
        "justified it and the residual it leaves.",
        "The provenance vocabulary each link must stamp on its output, so a "
        "coded join is never indistinguishable from an inference.",
        "The enumerated residual at every link, with claim volume, so the "
        "operator can decide what is acceptable rather than being told.",
        "A list of decisions that are the operator's and were not taken by "
        "the experiment.",
        "The measured difference between an affluent and a poor state at "
        "every link.",
    ]:
        doc.add_paragraph(item, style="List Bullet")

    # ---------------------------------------------------------------- hazards
    h(doc, "7. Hazards already proven, to be guarded against", 1)
    doc.add_paragraph(
        "Each of these produced a confident wrong answer during the molecule "
        "work. They are recorded so the later links do not repeat them.")
    table(doc,
          ["Hazard", "What it did"],
          [["Containment mistaken for identity",
            "Matching 'the label mentions potassium chloride' resolved Klor-Con to "
            "280 labels spanning 145 ingredients"],
           ["A substring matching inside a longer phrase",
            "'active ingredients:' matched inside 'INactive ingredients:' and would "
            "have keyed 244 labels to talc and lactose"],
           ["Role mistaken for identity",
            "A trials alias edge taught the bridge that dextrose is placebo, because "
            "dextrose IS the placebo arm in thousands of studies"],
           ["An unvalidated exclusion rule dominating the corpus",
            "A purpose-section rule written in one sitting now accounts for 116,425 "
            "of 168,954 OTC exclusions, against 47,693 from FDA's own declared flag. "
            "It has never been validated"],
           ["A denominator that does not exist",
            "Population figures were quoted against 206,303 and 168,000 labels. The "
            "real prescription corpus is 93,956"],
           ["Devices counted as failed drug matches",
            "162 pairs are pen needles, syringes and alcohol pads. They have no "
            "molecule and must leave the denominator, not depress the score"]],
          [2.6, 4.3])

    # ---------------------------------------------------------------- decisions
    h(doc, "8. Decisions reserved to the operator", 1)
    table(doc,
          ["Decision", "Why it is not the experiment's to take"],
          [["ICD-10 level 3, level 4, or both as the delivered granularity",
            "A clinical and product decision with regulatory consequence"],
           ["Whether an investigational trial condition may be presented as an "
            "indication at all",
            "Presenting a studied use as an approved one is a clinical claim"],
           ["Whether a clinically distinct formulation that shares an ingredient "
            "rolls up to the same molecule — pegylated liposomal doxorubicin "
            "versus doxorubicin, protein-bound versus plain paclitaxel",
            "Different dosing, toxicity and indications. Erasing the distinction "
            "changes what the product recommends"],
           ["The accuracy floor below which a state or specialty is not served",
            "If accuracy correlates with affluence, serving anyway and serving "
            "nothing are both decisions with consequences"],
           ["Whether the purpose-section OTC rule stands, narrows, or goes",
            "It currently removes 116,425 labels on no validation"]],
          [3.5, 3.4])

    doc.save(DOC)
    _log.info("%s", "figure ->", fig)
    _log.info("%s", "document ->", DOC)


main()
