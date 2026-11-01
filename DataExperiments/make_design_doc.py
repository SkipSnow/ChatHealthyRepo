"""Medicare Diagnostic Chain -- Design and Business Rules.

A NEW DOCUMENT, not a version of the experiment design. The experiment
design (Medicare_DiagnosticChain_ExperimentDesign_v1..v5) described what we
were going to find out. This describes what we decided, and every business
rule the chain enforces, in a register at the end.

It supersedes v5 on five points decided after v5 was generated:
  - indications originate ONLY in approved FDA labels
  - ICD-10 is published in WHO canonical form, dotted
  - ICD level is WHO character depth, never billability
  - the fourth collection is clinical trials, and it is excluded for now
  - staging collections are engineering artifacts; public ones carry one
    published schema each

It also drops the R1/R2/R5 route labels. Those were invented here and they
hide what each route is, which is the same defect as nicknaming a collection.
Every route is now named for what it does.
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
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

OUT_DIR = r"C:\CHATHealthyLLC\pipeline\ArchitectureDesignAndAudit"
DOC = os.path.join(OUT_DIR, "Medicare_DiagnosticChain_Design_v1.docx")
FIGDIR = r"C:\CHATHealthyLLC\DataExperiments"


def h(doc, text, level):
    p = doc.add_heading(text, level=level)
    for r in p.runs:
        r.font.color.rgb = RGBColor(0x1F, 0x38, 0x64)
    return p


def table(doc, headers, rows, widths=None, size=8.5):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    hdr = t.rows[0].cells
    for i, text in enumerate(headers):
        hdr[i].text = ""
        run = hdr[i].paragraphs[0].add_run(text)
        run.bold = True
        run.font.size = Pt(size)
        run.font.color.rgb = RGBColor(0x1F, 0x38, 0x64)
    for row in rows:
        cells = t.add_row().cells
        for i, text in enumerate(row):
            cells[i].text = ""
            run = cells[i].paragraphs[0].add_run(str(text))
            run.font.size = Pt(size)
    if widths:
        for r in t.rows:
            for i, w in enumerate(widths):
                if i < len(r.cells):
                    r.cells[i].width = Inches(w)
    return t


def _template():
    """Inherit the operator's footer by reusing the part rather than
    rebuilding it -- a rebuilt footer risks a near-match."""
    for name in ("Medicare_DiagnosticChain_ExperimentDesign_v2_anotated.docx",
                 "Medicare_DiagnosticChain_ExperimentDesign_v1_anotated.docx",
                 "Medicare_DiagnosticChain_ExperimentDesign_v5.docx",
                 "Medicare_DiagnosticChain_ExperimentDesign_v2.docx"):
        cand = os.path.join(OUT_DIR, name)
        if os.path.exists(cand):
            return cand
    raise ChatHealthyException("runtime_error", "no template found to inherit the footer from")


TEMPLATE = _template()


def open_from_template():
    doc = Document(TEMPLATE)
    body = doc.element.body
    sect = body.find(qn("w:sectPr"))
    for child in list(body):
        if child is not sect:
            body.remove(child)
    return doc


def chain_figure():
    """The chain, with each table's grain and every inference marked."""
    path = os.path.join(FIGDIR, "fig_design_chain.png")
    fig, ax = plt.subplots(figsize=(13.2, 3.5))
    ax.set_xlim(0, 13.2); ax.set_ylim(0, 3.5); ax.axis("off")
    boxes = [
        ("CMS Part D\nclaim rows", "one row per\nNPI x drug name", "#e8eef7", None),
        ("T1\nmolecule bridge", "one row per\n(brand, generic) pair", "#dce9f5",
         "resolution runs ONCE\nper pair, not per claim"),
        ("T2\nprovider x molecule", "one row per\n(NPI, molecule)", "#dce9f5",
         "billing address is the\n1:1 anchor, asserted"),
        ("T3\nmolecule x indication", "one row per\n(molecule, indication)",
         "#fde9d9", "INFERENCE: the label's\nprose becomes discrete\nindications"),
        ("T4\nindication x ICD", "one row per\n(indication, code)", "#fde9d9",
         "INFERENCE: coding, by\nthe benchmarked agent"),
        ("ProviderMedicare", "one record per\nmolecule per provider",
         "#dff0d8", "nesting only --\nno computation"),
    ]
    w, gap = 1.85, 0.37
    x = 0.25
    for label, grain, colour, note in boxes:
        ax.add_patch(patches.FancyBboxPatch(
            (x, 1.55), w, 1.15, boxstyle="round,pad=0.04",
            facecolor=colour, edgecolor="#1f3864", linewidth=1.3))
        ax.text(x + w / 2, 2.32, label, ha="center", va="center",
                fontsize=9.2, fontweight="bold", color="#1f3864")
        ax.text(x + w / 2, 1.85, grain, ha="center", va="center",
                fontsize=7.4, color="#404040")
        if note:
            red = note.startswith("INFERENCE")
            ax.text(x + w / 2, 1.02, note, ha="center", va="top", fontsize=7.0,
                    color="#c00000" if red else "#2e7d32",
                    fontweight="bold" if red else "normal")
        if x > 0.3:
            ax.annotate("", xy=(x - 0.03, 2.12), xytext=(x - gap + 0.03, 2.12),
                        arrowprops=dict(arrowstyle="-|>", color="#1f3864", lw=1.4))
        x += w + gap
    ax.text(6.6, 0.25, "Orange = cannot be produced without inference.   "
                       "Green = deterministic.   "
                       "Every inferred fact is capped at 0.9 confidence and "
                       "stamped provenance='inference'.",
            ha="center", fontsize=8.0, color="#404040")
    fig.savefig(path, dpi=200, bbox_inches="tight"); plt.close(fig)
    return path


# ======================================================================
# THE BUSINESS RULES REGISTER -- the formal capture at the end of the doc.
# Each rule carries where it came from and where it is enforced. A rule with
# no enforcement is listed as unenforced rather than quietly assumed.
# ======================================================================
RULES = [
    # id, rule, source, enforcement
    ("BR-001",
     "An indication may ONLY originate in an approved FDA label. A completed "
     "clinical trial may corroborate an indication but may never introduce "
     "one. Completion is not approval.",
     "Operator, 2026-10-30",
     "chain build: the trial rung is excluded as an originating source. "
     "Measured cost 0.68 claim points, 31 molecules of 969."),
    ("BR-002",
     "No experimental medicine and no not-yet-approved indication enters the "
     "data at all.",
     "Operator, 2026-10-30",
     "Follows from BR-001; the approved-origin filter is the only admission "
     "path, so no second rule is needed."),
    ("BR-003",
     "Confidence of exactly 1.0 is RESERVED for a deterministic derivation. "
     "When the derivation is a coded join we know the fact, and withholding "
     "certainty we hold is a misrepresentation by omission.",
     "Operator, 2026-10-30",
     "ENFORCED: the build asserts every non-inference row is exactly 1.0 and "
     "stops otherwise."),
    ("BR-004",
     "Anything inferred carries 0.0 to 0.9. NOTHING inferred ever reaches "
     "1.0. The band 0.91-0.99 is empty by construction, so the column is "
     "readable at a glance.",
     "Operator, 2026-10-30",
     "ENFORCED: the build asserts no inference row exceeds 0.9 and stops "
     "otherwise. Observed ceiling 0.891."),
    ("BR-005",
     "The inferred ceiling is applied ALGORITHMICALLY after the model "
     "returns, by multiplying by 0.9. It is never applied by telling the "
     "model a different scale, because the prompt's output contract belongs "
     "to the benchmark and changing it voids the measured scores.",
     "Operator, 2026-10-30",
     "Build multiplies on insert. Multiply rather than clamp: 55.4% of codes "
     "return at or above 0.9, so a clamp would collapse 4,227 of 7,635 rows "
     "onto one value."),
    ("BR-006",
     "Residual defect risk is carried by ONE global disclosure to the user "
     "-- the dataset is audited and a defect is always possible. It is never "
     "distributed into per-fact confidence as a discount, because nobody can "
     "quantify their own undiscovered defects.",
     "Operator, 2026-10-30",
     "Not a code rule. A publication rule, binding on the user-facing "
     "surface."),
    ("BR-007",
     "ICD-10 codes are PUBLISHED in WHO canonical form: dotted, with the "
     "decimal after the third character, in the range A00.0 to Z99.9.",
     "WHO ICD-10 Volume 2, verified 2026-10-30; operator ruling",
     "Published shape carries the dotted form. ICD-10-CM extends depth to 7 "
     "characters on 87.8% of its codes but never moves the decimal, so there "
     "is no conflict to resolve."),
    ("BR-008",
     "The dotless form is an INTERNAL join key only and is never published. "
     "It is what CMS's own distribution file uses, which is a transmission "
     "convention and not the standard.",
     "Operator ruling, 2026-10-30",
     "Carried as a separate column, indexed, used for every join."),
    ("BR-009",
     "ICD level is the code's CHARACTER DEPTH per WHO -- level 3 means three "
     "characters, level 4 means four. It is never a proxy such as "
     "billability.",
     "Design correction, 2026-10-30",
     "ENFORCED: computed from the code string. Previously conflated with "
     "is_billable, which stamped the five-character I50.81 as level 3 and "
     "mislabelled 466 rows."),
    ("BR-010",
     "The provider's BILLING address is the anchor and MUST be 1:1 with the "
     "NPI. The build stops if it is not, because a non-1:1 anchor makes "
     "every downstream count wrong and nothing later repairs it.",
     "Operator, 2026-10-30",
     "ENFORCED: asserted before use. Measured 513,777 rows before and after "
     "the provider join, ratio 1.0000."),
    ("BR-011",
     "The PRACTICE address is for end-user display only and never constructs "
     "a record. It is 1:many -- worst observed 356 addresses on one NPI -- so "
     "joining on it multiplies a provider by its locations.",
     "Operator, 2026-10-30",
     "Practice addresses held in their own table, never joined into the "
     "record grain."),
    ("BR-012",
     "Where a prescription was FILLED and where the provider's BILLING "
     "OFFICE sits are two distinct facts and are never substituted for one "
     "another.",
     "Operator, 2026-10-30",
     "Both carried as separate columns. They disagree for 1,656 providers in "
     "the two test states; any single 'state' column is wrong for those."),
    ("BR-013",
     "NUCC specialty comes from the NPI registry, not from the claim file's "
     "prescriber-type field. The registry is authoritative for what a "
     "provider IS.",
     "Operator, 2026-10-30",
     "Joined from the provider collection by NPI."),
    ("BR-014",
     "Claim volume is additive ONLY at (NPI, molecule). At any delivered "
     "grain it is non-additive, the column is NAMED non-additive, and every "
     "rollup distinct-counts back to the additive grain.",
     "Design, 2026-10-30",
     "ENFORCED by naming and by construction. The naive sum over the "
     "delivered set is inflated 53.5x -- 1,759,263,403 against a true "
     "32,870,482."),
    ("BR-015",
     "Never weight by beneficiary counts. HIPAA minimum-cell suppression "
     "fires below 11 beneficiaries and bites HARDER in provider-dense "
     "areas, so beneficiary weighting silently penalises the better data. "
     "Weight by claims.",
     "Measured finding, 2026-10",
     "Suppressed beneficiary counts stored as explicit NULL, never zero. No "
     "measure divides by them."),
    ("BR-016",
     "Molecule MATCHING folds salt to base, so a claim naming hydrocodone "
     "meets a label naming hydrocodone bitartrate. Molecule IDENTITY "
     "preserves the precise ingredient set, so metoprolol succinate never "
     "merges with metoprolol tartrate.",
     "Operator ruling: 'the answer is no'",
     "Fold used for the match; the unfolded set is the identity. RxNorm edge "
     "is has_form, verified -- not precise_ingredient_of, which relates "
     "brand to ingredient and cost 1.90 claim points when wrong."),
    ("BR-017",
     "A fact that is UNSUPPORTED is removed, not discounted. Confidence is "
     "the wrong instrument for a wrong fact: a wrong fact at 0.6 is still a "
     "wrong fact in the data.",
     "Design, 2026-10-30",
     "Admission rules reject; nothing is admitted with a lowered score in "
     "place of a correctness decision."),
    ("BR-018",
     "Provenance records REPRODUCIBILITY, not accuracy. A delivered fact "
     "inherits the WEAKEST provenance on its path. Vocabulary: "
     "joined_coded_source, adopted_vocabulary, inference.",
     "Design; demonstrated 2026-10-30",
     "Computed per delivered fact. The demonstration: a fully deterministic "
     "route produced the chain's least accurate codes while the inference "
     "produced the most precise."),
    ("BR-019",
     "A RELATEDNESS crosswalk may not assert specificity the indication does "
     "not support. A vocabulary answering 'which codes relate to this "
     "concept' is not answering 'which code IS this indication'.",
     "PROPOSED -- operator ruling outstanding",
     "NOT YET ENFORCED. The NIH vocabulary crosswalk returns the whole I50 "
     "family for 'Heart Failure', so 108 molecules asserted right heart "
     "failure across 9,542,476 claims. Removing that route costs 0.08 claim "
     "points and 15 molecules."),
    ("BR-020",
     "Cohort notes -- pediatric use, geriatric use, pregnancy, nursing, "
     "specific populations, contraindications, boxed warning -- are stored "
     "VERBATIM and never interpreted by the pipeline. Storing the label's "
     "sentence is a fact; reading it is an inference.",
     "Operator, 2026-10-30",
     "Stored per label. 937 of 938 molecules carry at least one; 910 carry a "
     "pediatric-use section."),
    ("BR-021",
     "Population filtering is a SERVING-TIME concern, not a pipeline fact. "
     "The record states what the label approves for whom; hiding a molecule "
     "from the parent of a child is a query decision, because a paediatrician "
     "and that parent need the same record filtered differently.",
     "RECOMMENDED -- operator ruling outstanding",
     "NOT ENFORCED either way. Recorded so the decision is visible rather "
     "than absent."),
    ("BR-022",
     "A fact that cannot be derived is ABSENT, never defaulted. No false, no "
     "zero, no empty string standing in for an answer we do not have.",
     "Standing requirement across the pipeline",
     "Null throughout. Distinguished from BR-003: absent is right for a fact "
     "we could not derive and wrong for one we derived with certainty."),
    ("BR-023",
     "Model output is validated against a CLOSED code catalog. Any code "
     "outside it is rejected. This is what makes hallucination measurable "
     "rather than estimated.",
     "Benchmark design, EPIC-010-F-007-S-008",
     "ENFORCED against the 12,014-code catalog read through the tool that "
     "fed the model -- not re-parsed, since a second parser is a second "
     "answer. Observed: 1 fabricated code in 1,525 distinct (0.066%)."),
    ("BR-024",
     "ProviderMedicare is keyed by NPI and molecule -- one record per "
     "molecule per provider. The indication and code fan-out NESTS inside "
     "the record rather than multiplying rows.",
     "EPIC-010-F-007-S-002",
     "Grain fixed by the approved story. Nesting keeps the national estimate "
     "near 26M documents instead of the ~10^9 a provider-by-code grain "
     "would produce."),
    ("BR-025",
     "Every PUBLIC collection has exactly ONE schema, and it is published on "
     "the ChatHealthy website.",
     "Operator, 2026-10-30",
     "Publication rule. Binding on every collection the application reads."),
    ("BR-026",
     "STAGING collections are engineering artifacts. They are specified and "
     "schema'd internally and are never published.",
     "Operator, 2026-10-30",
     "Internal schemas declared with each staging table."),
    ("BR-027",
     "The reference implementation defines WHAT, never HOW. The pipeline "
     "must reproduce its OUTPUT, not its method -- a relational join is a "
     "legitimate mechanism for the reference and not a constraint on the "
     "pipeline.",
     "Operator, 2026-10-30",
     "The reference is preserved as a QA artifact; when the design changes, "
     "it changes with it."),
    ("BR-028",
     "The port from relational to documents performs NESTING ONLY. No "
     "computation, no defaulting, no filtering, no meaning-bearing rename. A "
     "field the contract needs but no column holds FAILS the port rather "
     "than being computed in transit.",
     "Operator, 2026-10-30",
     "Port reads a view whose column names ARE the document paths, so there "
     "is no mapping layer in which invention could hide."),
]


def main():
    doc = open_from_template()

    # ---------------------------------------------------------------- title
    t = doc.add_heading("Medicare Diagnostic Chain", level=0)
    for r in t.runs:
        r.font.color.rgb = RGBColor(0x1F, 0x38, 0x64)
    sub = doc.add_paragraph("Design and Business Rules").runs[0]
    sub.font.size = Pt(13); sub.bold = True
    sub.font.color.rgb = RGBColor(0x1F, 0x38, 0x64)
    r = doc.add_paragraph("Version 1 \u00b7 2026-10-30 \u00b7 "
                          "supersedes Experiment Design v1-v5").runs[0]
    r.font.size = Pt(9.5); r.font.color.rgb = RGBColor(0x60, 0x60, 0x60)

    # ================================================================== 1
    h(doc, "1. What this document is", 1)
    doc.add_paragraph(
        "The experiment design documents said what we were going to find "
        "out. This says what we decided, and it carries every business rule "
        "the chain enforces in a formal register at the end. Where a rule is "
        "not yet enforced, the register says so rather than implying it.")
    doc.add_paragraph(
        "It supersedes Experiment Design v5 on five points settled after v5 "
        "was written: indications originate only in approved FDA labels; "
        "ICD-10 publishes in WHO canonical dotted form; ICD level is WHO "
        "character depth and not billability; the fourth collection is "
        "clinical trials and it is excluded for now; and staging collections "
        "are engineering artifacts while every public collection carries one "
        "published schema.")
    doc.add_paragraph(
        "IT ALSO DROPS THE R1/R2/R5 ROUTE LABELS. Those were invented in the "
        "experiment and they hide what each route is, which is the same "
        "defect as nicknaming a collection: the reader cannot see from the "
        "name whether a fact came from Medicare's own policy or from a model "
        "reading a label. Every route is now named for what it does.")
    doc.add_paragraph(
        "WHAT IS DESIGN AND WHAT IS REQUIREMENT. Several rules in the "
        "register were decided by the operator and are REQUIREMENTS; they "
        "are recorded here as given and the design answers to them. They "
        "still need to land in the agile backlog, and that is the operator's "
        "to author, not this document's.")

    # ================================================================== 2
    h(doc, "2. The chain", 1)
    doc.add_picture(chain_figure(), width=Inches(9.6))
    doc.add_paragraph(
        "Five tables, each at its own grain, each a separate object. They "
        "are not stages inside one derivation, for three reasons that each "
        "cost something real if ignored.")
    table(doc,
          ["Why separate", "What it costs to merge them"],
          [["Grain",
            "Five grains cannot live in one table without multiplying rows, "
            "which is the fan-out defect relocated rather than removed"],
           ["Attributable failure",
            "The indication hop and the coding hop are split precisely so a "
            "wrong code can be traced to a wrong INDICATION versus a wrong "
            "CROSSWALK. Collapsed into one hop that question is "
            "unanswerable"],
           ["Provenance",
            "Each link stamps its own and the delivered record inherits the "
            "weakest on its path. That is only expressible if the links are "
            "separate objects"]],
          [1.9, 7.4])
    doc.add_paragraph(
        "The molecule bridge is where the economics sit. Resolution runs "
        "once per distinct (brand name, generic name) pair, not per claim: "
        "28,023,892 national claim rows collapse to 3,159 pairs, about "
        "1/8,870th the work. It is also the only form in which the "
        "resolution is AUDITABLE -- 3,159 decisions a person can read, "
        "against a per-row resolution nobody can.")

    # ================================================================== 3
    h(doc, "3. The routes to a diagnosis code, named for what they are", 1)
    table(doc,
          ["Route", "What it actually is", "Provenance", "Level it may assert"],
          [["medicare_coverage_policy",
            "CMS publishes which diagnoses it covers for each procedure; we "
            "join to that policy",
            "joined_coded_source",
            "as published"],
           ["nih_vocabulary_crosswalk",
            "Diseases carry MeSH codes (the NIH controlled vocabulary). The "
            "NIH cross-reference service is asked which ICD-10 codes "
            "correspond",
            "joined_coded_source",
            "DISPUTED -- see BR-019. It answers RELATEDNESS, not identity"],
           ["icd_description_match",
            "The indication text matches an ICD code's official description",
            "adopted_vocabulary",
            "as matched"],
           ["ccsr_clinical_grouping",
            "Groups a code into a clinical category and body system. An "
            "enrichment, not a code source",
            "adopted_vocabulary",
            "n/a"],
           ["fda_label_coded_by_model",
            "The benchmarked agent reads the FDA label's own indication "
            "prose and codes what it says",
            "inference",
            "3 and 4, from the text"]],
          [2.3, 3.5, 1.7, 2.0])
    doc.add_paragraph(
        "THE ONE ROUTE THAT CAN BE QUOTED TO A USER is the model's, because "
        "it is the only one that read the source text -- so the label's own "
        "sentence can be shown. Medicare's policy can be cited. The NIH "
        "crosswalk is the one for which no honest user-facing sentence "
        "exists at four-character specificity, since the only true statement "
        "is 'the vocabulary relates this code to that concept'.")

    # ================================================================== 4
    h(doc, "4. Trust: provenance and confidence are different axes", 1)
    doc.add_paragraph(
        "Every derived fact makes two separate statements about itself. One "
        "says how it was produced. The other says how sure we are it is "
        "right. They are independent, and this chain demonstrated that they "
        "point in opposite directions.")
    table(doc,
          ["Axis", "Values", "Answers", "Does NOT answer"],
          [["provenance",
            "joined_coded_source | adopted_vocabulary | inference",
            "How reproducible is this fact?",
            "Whether it is CORRECT"],
           ["confidence",
            "exactly 1.0, or 0.0 to 0.9",
            "How sure are we it is right?",
            "How it was produced -- that is provenance's job"]],
          [1.3, 2.6, 2.6, 2.9])
    doc.add_paragraph(
        "THE MEASURED COUNTER-EXAMPLE. The NIH vocabulary crosswalk is "
        "perfectly deterministic -- same input, same output, published "
        "authority. For the descriptor Heart Failure it returns I50, I50.1, "
        "I50.81, I50.810 and I50.9. 108 molecules carry that descriptor, so "
        "all five codes inherited an IDENTICAL set of 130,404 "
        "provider-molecule pairs and an identical 9,542,476 claims. The "
        "record therefore asserts that drugs indicated for heart failure "
        "treat RIGHT HEART FAILURE, UNSPECIFIED. They do not.")
    doc.add_paragraph(
        "Meanwhile the model, reading the label, coded 'reduced ejection "
        "fraction' to systolic failure and 'left-ventricular dysfunction' to "
        "left heart failure -- narrow and correct, 9 and a handful of "
        "molecules rather than 108.")
    doc.add_paragraph(
        "TWO CONSEQUENCES. The provenance mix flatters itself: a high "
        "coded-source share is not evidence of a high-quality chain when "
        "part of that share is this defect. And confidence is the wrong "
        "instrument for a wrong fact -- the row is removed, not discounted "
        "(BR-017).")
    doc.add_paragraph(
        "NIH published that mapping correctly. The defect is in consuming a "
        "relatedness mapping as an equivalence mapping -- the same error that "
        "made the ICD-9 to ICD-10 transition painful, where the General "
        "Equivalence Mappings were published as approximate and widely "
        "consumed as exact.")

    # ================================================================== 5
    h(doc, "5. What may enter: approval is the admission gate", 1)
    doc.add_paragraph(
        "An indication may only originate in an approved FDA label. A "
        "completed clinical trial may corroborate one but may never "
        "introduce one, because completion is not approval -- a failed trial "
        "and a successful one look identical in a trial-sourced indication.")
    doc.add_paragraph(
        "This single rule also disposes of the pediatric-trial case without "
        "a second rule: an ongoing trial for children cannot introduce a "
        "pediatric indication, because no trial introduces anything.")
    table(doc,
          ["Measured consequence", "Figure"],
          [["Claim coverage, trials admitted as an origin", "90.19%"],
           ["Claim coverage, approved origin only", "89.51%"],
           ["Cost of the rule", "0.68 claim points, 31 molecules of 969"],
           ["Share of delivered facts previously marked investigational",
            "61% -> zero"]],
          [5.4, 3.0])

    # ================================================================== 6
    h(doc, "6. ICD-10 conformance", 1)
    doc.add_paragraph(
        "ICD-10 is maintained by the WORLD HEALTH ORGANIZATION. ICD-10-CM is "
        "the United States Clinical Modification, maintained by CDC/NCHS, "
        "with CMS distributing the code files for billing. It is not an ISO "
        "standard.")
    doc.add_paragraph(
        "WHO Volume 2 states the structure: 'Most of the three-character "
        "categories are subdivided by means of a fourth, numeric character "
        "after a decimal point... Possible code numbers therefore range from "
        "A00.0 to Z99.9.' So the canonical written form carries the decimal.")
    table(doc,
          ["Fact", "Measured or cited"],
          [["Does ICD-10-CM stay inside WHO's four-character structure?",
            "No. 86,172 of 98,186 codes (87.8%) are 5, 6 or 7 characters"],
           ["Does it move the decimal?",
            "No. A01.00, C44.1021 -- always after the third character. The "
            "structural convention is honoured and there is no conflict"],
           ["Which form does CMS distribute?",
            "DOTLESS in icd10cm_codes_2026.txt (A000, A0100). That is a "
            "transmission convention, not the standard"],
           ["Which form do we publish?",
            "DOTTED, per WHO (BR-007)"],
           ["Which form do we join on?",
            "DOTLESS, internal only (BR-008)"],
           ["What does our chain emit?",
            "Levels 3 and 4 only -- 12,014 of the 98,186 codes -- so "
            "everything delivered sits inside WHO's own structure. Level 3 "
            "is the level WHO designates mandatory for international "
            "comparison"]],
          [3.4, 5.9])
    doc.add_paragraph(
        "A CORRECTION RECORDED SO IT CANNOT RECUR. Level was previously "
        "derived from BILLABILITY rather than character depth, which stamped "
        "the five-character I50.81 as level 3 and mislabelled 466 rows "
        "across three routes. Level is now computed from the code string "
        "(BR-009). Any earlier level-3 or level-4 figure that mixed routes "
        "is unreliable.")
    doc.add_paragraph(
        "It also explains the chain's one fabricated code. WHO states that "
        "the fourth character '.9 is mostly used to convey the same meaning "
        "as the three-character category title'. The model wrote N34.9 for "
        "urethritis, following WHO's own documented convention into one of "
        "the categories that departs from it -- N34 stops at .3, and .3 is "
        "its unspecified slot. A documented pattern misapplied, not an "
        "invention. The closed catalog caught it (BR-023).")

    # ================================================================== 7
    h(doc, "7. The provider record: one provider, one row", 1)
    doc.add_paragraph(
        "The same NPI prescribing the same drug appears on more than one "
        "claim row. Normalising to one provider requires a canonical map of "
        "NPI to address, and only one of the two addresses a provider holds "
        "can be that map.")
    table(doc,
          ["Address", "Cardinality with NPI", "Role"],
          [["Business (billing)", "ONE to ONE",
            "THE ANCHOR. The record is constructed on it. Asserted 1:1 "
            "before use and the build STOPS if it is not"],
           ["Practice", "ONE to MANY -- worst observed 356 on one NPI",
            "Shown to the end user. Never constructs the record; joining on "
            "it multiplies a provider by its locations"]],
          [1.9, 2.7, 4.7])
    doc.add_paragraph(
        "Measured: 513,777 rows before the provider join and 513,777 after, "
        "ratio 1.0000. And provider coverage of the claim file is complete "
        "-- all 18,759 distinct prescriber NPIs matched a provider record "
        "and all 18,759 carry a billing address, matched on NPI alone.")
    doc.add_paragraph(
        "TWO STATE FACTS, NEVER SUBSTITUTED. Where a prescription was FILLED "
        "is a CMS fact about the claim. Where the provider's BILLING OFFICE "
        "sits is a registry fact about the provider. They disagree for 1,656 "
        "providers in two small states, so any single 'state' column is "
        "wrong for those and the error is invisible once committed.")

    # ================================================================== 8
    h(doc, "8. Volume: additive in exactly one place", 1)
    doc.add_paragraph(
        "Claim volume belongs to (NPI, molecule). The delivered set fans out "
        "from there -- one molecule has many indications, one indication has "
        "many codes -- so the volume repeats across the fan-out and summing "
        "it is wrong.")
    table(doc,
          ["Measure", "Figure"],
          [["Naive sum over every delivered row", "1,759,263,403"],
           ["True claim total at its additive grain", "32,870,482"],
           ["Inflation factor", "53.5x"]],
          [5.0, 3.0])
    doc.add_paragraph(
        "NO FRACTIONAL ALLOCATION. No requirement states a weighting rule, "
        "and inventing one would put an unapproved rule in the data where it "
        "would afterwards be indistinguishable from a specified one. Instead "
        "the column is NAMED non-additive so a sum over it is visibly rather "
        "than quietly wrong, and every rollup distinct-counts back to (NPI, "
        "molecule). A molecule reaching one code through two indications "
        "counts once.")

    # ================================================================== 9
    h(doc, "9. Cohort notes: stored verbatim, interpreted elsewhere", 1)
    doc.add_paragraph(
        "The FDA label record carries 122 top-level fields. The pipeline "
        "previously kept 12 and discarded 110, among them every section "
        "stating WHICH POPULATION a medicine is for. Those sections are now "
        "kept.")
    table(doc,
          ["Section", "Present on"],
          [["contraindications", "35.5% of labels"],
           ["pediatric_use", "28.8%"],
           ["pregnancy", "28.6%"],
           ["geriatric_use", "25.3%"],
           ["use_in_specific_populations", "21.1%"],
           ["pregnancy_or_breast_feeding", "18.6%"],
           ["nursing_mothers", "15.5%"],
           ["boxed_warning", "13.5%"],
           ["Molecules gaining at least one cohort note",
            "937 of 938 (99.9%)"],
           ["Molecules with a pediatric-use section", "910"]],
          [4.4, 3.4])
    doc.add_paragraph(
        "STORED, NOT READ. The label's own sentence is a fact; deciding what "
        "it implies is an inference. So the prose is held verbatim and "
        "quotable -- 'Amphetamines are not recommended for use in children "
        "under 3 years of age' -- and no pipeline step converts it into a "
        "boolean.")
    doc.add_paragraph(
        "AGE AND SEX ARE NOT SYMMETRIC. There is no sex or gender field in "
        "the record. Sex-specific restriction lives inside the prose of "
        "use_in_specific_populations and contraindications. So age arrives "
        "as a section that can be read directly while sex requires "
        "interpreting narrative, which puts it in the inference column. The "
        "two must not be presented as equally well-founded.")

    # ================================================================== 10
    h(doc, "10. The reference implementation and the port", 1)
    doc.add_paragraph(
        "The information architecture is worked out relationally and then "
        "ported to documents. The reason is not preference: a relational "
        "schema will not let the grain, the key or the cardinality stay "
        "implicit, and it holds you to all three. A document store lets all "
        "three be deferred, and the billing-anchor defect is the proof -- "
        "'513,777 before, 513,777 after' is an assertion that can FAIL, and "
        "in a document store that defect hides.")
    table(doc,
          ["What ports mechanically", "What must still be designed"],
          [["Grain, key, cardinality, provenance, every field value",
            "THE ACCESS PATH. Joins do not exist in the document store; the "
            "delivered record is a three-table join relationally and must "
            "become nesting-at-write"],
           ["The business logic, unchanged",
            "GLOBAL VERSUS PARTITIONED. The molecule bridge is global -- one "
            "bridge for the country. A geography-partitioned run would "
            "rebuild it per partition and discard the saving that is its "
            "entire purpose"],
           ["The document shape, as a rendering decision",
            "SCALE. Two states are 554,317 claim rows against 28,023,892 "
            "nationally. The approved grain -- one record per molecule per "
            "provider -- keeps this near 26M documents; a provider-by-code "
            "grain would reach the order of 10^9"]],
          [3.3, 6.0])
    doc.add_paragraph(
        "THE PORT PERFORMS NESTING ONLY (BR-028). It reads a view whose "
        "column names ARE the document paths, so no mapping layer exists in "
        "which invention could hide. No computation, no defaulting, no "
        "filtering. A field the contract needs but no column holds FAILS the "
        "port; the gap is then closed in the relational layer where it is "
        "visible and reviewable, never in transit.")
    doc.add_paragraph(
        "THE REFERENCE DEFINES WHAT, NOT HOW (BR-027). It is preserved as a "
        "QA artifact and changes when the design changes. The pipeline must "
        "reproduce its OUTPUT; a relational join is a legitimate mechanism "
        "for the reference and no constraint on the pipeline.")

    # ================================================================== 11
    h(doc, "11. Collections: what is published and what is not", 1)
    table(doc,
          ["Collection", "Standing", "Schema"],
          [["ProviderMedicare", "PUBLIC. Keyed by NPI and molecule",
            "One schema, published on the ChatHealthy website"],
           ["SpecialtyMedicare",
            "PUBLIC. A rollup to the NUCC specialty -- and because "
            "aggregation IS business logic, the rollup must exist "
            "relationally before anything is ported",
            "One schema, published"],
           ["NormalizedIndicationMap", "PUBLIC, currently deferred in the "
            "backlog", "One schema, published, when undeferred"],
           ["Clinical trials", "PUBLIC eventually. EXCLUDED for now by "
            "operator decision", "One schema, published, when admitted"],
           ["The five intermediary tables",
            "STAGING. Engineering artifacts",
            "Specified and schema'd internally; never published"]],
          [2.2, 4.1, 3.0])

    # ================================================================== 12
    h(doc, "12. Measurement", 1)
    table(doc,
          ["Measure", "Why it is required"],
          [["Share of CLAIM VOLUME resolved",
            "the business-weighted answer"],
           ["Share of DISTINCT ITEMS resolved",
            "the tail; a high volume figure hides hundreds of failed rare "
            "medicines"],
           ["PRECISION by inspecting matches, not counting them",
            "a containment defect once matched 280 labels across 145 "
            "ingredients and reported success"],
           ["Provenance mix at the DELIVERED grain",
            "a chain that is mostly inference is a different product from "
            "one that is mostly joined -- and per section 4, a high coded "
            "share is not by itself a quality signal"],
           ["Hallucination against the CLOSED catalog",
            "measurable rather than estimated, because the catalog is the "
            "set the model was given"],
           ["The residual enumerated item by item with its volume",
            "an unenumerated residual is an unexamined one"]],
          [3.2, 6.1])

    # ============================================================== register
    sec = doc.add_section()
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = sec.page_height, sec.page_width
    sec.left_margin = sec.right_margin = Inches(0.55)

    h(doc, "Appendix A \u2014 Business Rules Register", 1)
    doc.add_paragraph(
        "Every business rule the chain enforces, with its source and where "
        "it is enforced. A rule that is NOT enforced says so. Two are "
        "recorded as outstanding operator decisions rather than settled, so "
        "the gap is visible in the register rather than absent from it.")
    table(doc,
          ["ID", "Rule", "Source", "Enforcement"],
          [[rid, rule, src, enf] for rid, rule, src, enf in RULES],
          [0.65, 4.3, 1.7, 4.3], size=7.6)

    doc.add_paragraph()
    h(doc, "A.1 Outstanding operator decisions", 2)
    table(doc,
          ["Rule", "The question", "What it turns on"],
          [["BR-019",
            "May a relatedness crosswalk assert a four-character code?",
            "9,542,476 claims currently carry right-heart-failure codes on "
            "the strength of an indication that says heart failure. Removing "
            "the route costs 0.08 claim points and 15 molecules of 968. The "
            "route reaches 1,423 codes nothing else reaches, but those carry "
            "the 0.08 points -- they are the long tail"],
           ["BR-021",
            "Does population filtering live in the pipeline or at serving "
            "time?",
            "Recommendation is serving time. Baked into the data, one record "
            "can only serve one audience"]],
          [0.9, 3.4, 8.6], size=7.8)

    doc.save(DOC)
    _log.info("%s", "document ->", DOC)
    _log.info("%s", "business rules in register:", len(RULES))


main()
