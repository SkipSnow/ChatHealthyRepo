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
import sys
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as patches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from docx import Document  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.enum.section import WD_ORIENT  # noqa: E402
from docx.shared import Inches, Pt, RGBColor  # noqa: E402

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

OUT_DIR = r"C:\CHATHealthyLLC\pipeline\ArchitectureDesignAndAudit"
DOC = os.path.join(OUT_DIR, "Medicare_DiagnosticChain_ExperimentDesign_v2.docx")
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


TEMPLATE = os.path.join(
    OUT_DIR, "Medicare_DiagnosticChain_ExperimentDesign_v1_anotated.docx")


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
    r = doc.add_paragraph("Version 2 · 2026-10-30 · local experiment").runs[0]
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
            "the deterministic attribute chain, and the known-before / "
            "known-after figure for each stage", "normative"],
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
          [["Provider → prescribed drug", "yes, authoritative", "100%", "§4.2"],
           ["Prescribed drug → molecule", "yes, measured", "97.89%", "§4.3"],
           ["Molecule → indication", "no", "unmeasured", "§4.5"],
           ["Indication → ICD-10 L3/L4", "no", "unmeasured", "§4.6"]],
          [2.6, 1.6, 1.5, 1.4])

    # =================================================================== §4
    h(doc, "4. Specification", 1)

    h(doc, "4.1 Tables and grain", 2)
    doc.add_paragraph(
        "28,023,892 claim rows collapse to 3,159 distinct (Brnd_Name, "
        "Gnrc_Name) pairs. Resolution therefore runs once per pair and is "
        "reused by every claim: the bridge is 1/8,870th the size of the claim "
        "file. Claims never join to molecules directly.")
    table(doc,
          ["Table", "Grain", "Rows", "Built from"],
          [["T1 cms_drug_resolution", "one per (Brnd_Name, Gnrc_Name)",
            "3,159 national / 1,910 DE+AL", "Part D + RxNorm + FDA + Trials"],
           ["T2 provider_molecule", "one per (Prscrbr_NPI, molecule_id)",
            "to be measured", "Part D claims ⋈ T1"],
           ["T3 molecule_indication", "one per (molecule_id, indication_id, source)",
            "to be measured", "FDA label prose + Trials conditions"],
           ["T4 indication_icd", "one per (indication_id, icd10_code)",
            "to be measured", "Coverage + ICD-10-CM + CCSR + MeSH + inference"],
           ["T5 delivered", "grain is a decision, see §4.7", "to be measured",
            "T2 ⋈ T3 ⋈ T4"]],
          [2.0, 2.3, 1.6, 2.1])

    h(doc, "4.2 Hop 1 — Part D row to T1 and T2 keys", 2)
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

    h(doc, "4.3 Hop 2 — T1, the deterministic attribute chain to the molecule", 2)
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

    h(doc, "4.3.1 Known before and known after, per stage", 3)
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

    h(doc, "4.4 Elimination rules, stated per collection", 2)
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

    h(doc, "4.5 Hop 3 — molecule to NORMALISED indication", 2)
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

    h(doc, "4.5.1 The normalisation inference — two distinct jobs", 3)
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

    h(doc, "4.6 Hop 4 — normalised indication to ICD-10 level 3 and 4", 2)
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
           ["R2", "MeSH descriptor id → UMLS CUI → ICD10CM code "
            "(REQUIRES MRCONSO, not yet loaded)", "joined_coded_source"],
           ["R3", "indication text → icd10cm_codes_2026.txt description",
            "adopted_vocabulary"],
           ["R4", "ICD-10-CM code → DXCCSR_v2026-1.csv 'CCSR CATEGORY n' for "
            "the clinical rollup and body system", "adopted_vocabulary"],
           ["R5", "indication text → code, by language model", "inference"]],
          [0.7, 4.8, 2.2])
    doc.add_paragraph(
        "Every T4 row stamps its route. The experiment reports what share of "
        "the delivered set rests on each, because a chain that is mostly R5 is "
        "a different product from one that is mostly R1, and the difference "
        "must be visible rather than discovered.")

    h(doc, "4.7 T5 — the delivered grain", 2)
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
           ["UMLS MRCONSO", "NOT LOADED",
            "required for R2 (MeSH → ICD-10). The UMLS API key exists. This is "
            "the one named gap in the specification"]],
          [2.2, 4.4, 3.8], size=8)
    doc.add_paragraph(
        "The format mismatch is load-bearing: coverage codes are dotted, "
        "ICD-10-CM and CCSR codes are dotless. One canonical form must be "
        "chosen and every source normalised to it before any join is "
        "attempted, or the hop silently returns nothing.")

    doc.save(DOC)
    _log.info("%s", "document ->", DOC)


main()
