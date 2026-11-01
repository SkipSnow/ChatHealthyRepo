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
DOC = os.path.join(OUT_DIR, "Medicare_DiagnosticChain_Design_v2.docx")
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
     "Operator approved 2026-10-30, on a measured cost of ZERO",
     "ENFORCED and ASSERTED. The crosswalk may assert the CATEGORY and never "
     "a specific child. Measured: dropping the unsupported specificity costs "
     "0.00 claim points -- every provider-molecule pair reaching one of those "
     "443 codes already reaches a supported code by another path, so the "
     "exclusion removes 2,129,611 rows of false precision and no coverage at "
     "all. Removed rather than discounted, per BR-017."),
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
    ("BR-029",
     "A source cached in multiple download generations is deduplicated to "
     "ONE row per natural key at its LATEST version. An older generation's "
     "content is superseded, not alternative.",
     "Design, 2026-10-30",
     "ENFORCED by a UNIQUE index rather than by convention. The FDA cohort "
     "table held 299,580 rows over 128,395 labels -- 2.33 generations of the "
     "same 14-file bulk set. Deduped to 128,395 with molecule coverage "
     "unchanged, so the duplicates were pure redundancy."),
    ("BR-030",
     "Every fact the port must emit is a COLUMN, never a key inside a JSON "
     "blob. Surfacing an existing JSON key as a column is mechanical; "
     "parsing one during the port would be computation and is forbidden by "
     "BR-028.",
     "Design, 2026-10-30",
     "ENFORCED. The NUCC vocabulary was a single JSONB blob and would have "
     "blocked the port entirely; its keys are now columns -- display name, "
     "grouping, classification, specialization, definition, section, notes "
     "and the four derived flags."),
    ("BR-031",
     "A model-returned code that cannot be resolved in the official code set "
     "is REMOVED, not published. It cannot acquire a canonical dotted form, "
     "so publishing it would both assert a diagnosis that does not exist and "
     "violate BR-007.",
     "Design, 2026-10-30",
     "ENFORCED by an inner join to the official set. One code was rejected "
     "-- N34.9, which does not exist: N34 stops at .3 and .3 is its "
     "unspecified slot. Hallucination after rejection is 0.0000, matching "
     "the benchmark exactly."),
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

    # ================================================================== 12A
    h(doc, "12A. MEASURED RESULT -- ICD-10 assignment", 1)
    doc.add_paragraph(
        "Medicare Part D, Delaware and Alabama. Every figure below is "
        "measured. Nothing is projected. Where a number is a ceiling rather "
        "than a result it is labelled as one.")
    doc.add_paragraph(
        "THREE DENOMINATORS, BECAUSE THEY ANSWER DIFFERENT QUESTIONS. Records "
        "count rows in the file. Claims count prescription FILLS. Days supply "
        "counts therapeutic EXPOSURE -- and it is the only one of the three "
        "that measures what a person actually took, because a 90-day statin "
        "fill and a 7-day antibiotic are both one claim.")
    table(doc,
          ["", "Records", "%", "Claims", "%", "Days supply", "%"],
          [["TOTAL IN FILE", "554,317", "100.00", "33,534,542", "100.00",
            "1,725,195,125", "100.00"],
           ["ASSIGNED: matched before the join fixes", "489,890", "88.38",
            "29,423,041", "87.74", "1,537,393,715", "89.11"],
           ["ASSIGNED: recovered by the folded salt key and brand rungs",
            "43,773", "7.90", "3,358,215", "10.01", "166,214,437", "9.63"],
           ["TOTAL ASSIGNED", "533,663", "96.27", "32,781,256", "97.75",
            "1,703,608,152", "98.75"],
           ["NOT ASSIGNED", "20,654", "3.73", "753,286", "2.25",
            "21,586,973", "1.25"]],
          [2.9, 1.0, 0.6, 1.1, 0.6, 1.3, 0.6], size=7.8)
    doc.add_paragraph(
        "DAYS SUPPLY IS THE STRONGEST OF THE THREE, and that is not luck. "
        "What the chain fails to code is short-duration: a vaccine is a "
        "single dose, so 160,770 unassigned claims amount to 333,042 days -- "
        "0.02%. What it does code is chronic therapy, which is what a "
        "provider's practice pattern consists of.")

    h(doc, "12A.1 What the two join fixes were worth", 2)
    doc.add_paragraph(
        "Row two of that table -- 7.90% of records, 10.01% of claims, 9.63% "
        "of days supply -- was being discarded for key-format reasons alone, "
        "with the labels present the whole time.")
    table(doc,
          ["Defect", "What it cost"],
          [["The label match joined the UNFOLDED identity key instead of the "
            "folded match key, in direct contradiction of the comment above "
            "it. CMS held atorvastatin as the base; the labels held the salt. "
            "BR-016 requires the fold for MATCHING and forbids it for "
            "IDENTITY, and this was a match that used identity.",
            "2,847,156 claims. Atorvastatin alone -- 1.3M claims, the single "
            "largest drug in the file -- reached no diagnosis code at all"],
           ["No brand rung on the label match, although brand-exact is "
            "already a sanctioned rung of the resolution ladder.",
            "117 molecules including Trelegy, Lantus, Biktarvy, Paxlovid and "
            "Anoro -- products whose labels were in the corpus and whose "
            "brands are distinctive"],
           ["CMS appends the delivery device to the brand name -- 'Lantus "
            "Solostar' against the label's 'Lantus'. No suffix strip existed.",
            "13 further molecules"]],
          [5.0, 4.3])

    h(doc, "12A.2 What remains unassigned, by cause", 2)
    table(doc,
          ["Cause", "Records", "Claims", "Days supply", "% days"],
          [["CMS truncates its generic-name field at 30 characters",
            "11,350", "364,266", "12,372,661", "0.72"],
           ["Not a drug -- devices and supplies. No indication EXISTS to code; "
            "correctly excluded rather than missing",
            "4,059", "95,594", "5,497,080", "0.32"],
           ["Molecule identified, no label found", "2,234", "89,011",
            "1,821,184", "0.11"],
           ["Unknown to the vocabularies", "1,666", "43,430", "1,556,256",
            "0.09"],
           ["Vaccine or biologic -- absent from the label source entirely",
            "1,334", "160,770", "333,042", "0.02"],
           ["Label in hand, coding pass incomplete", "11", "215", "6,750",
            "0.00"],
           ["TOTAL", "20,654", "753,286", "21,586,973", "1.25"]],
          [4.2, 1.1, 1.3, 1.6, 0.8], size=7.8)
    doc.add_paragraph(
        "THE 30-CHARACTER TRUNCATION IS CMS's, NOT OURS. The data dictionary "
        "defines Gnrc_Name as the 'USAN Generic Name - Short Version', and "
        "both name fields are derived by linking the NDC codes on the "
        "prescription drug event records to a commercial drug information "
        "database. The longest generic name anywhere in the file is exactly "
        "30 characters. Medicare itself never faces this problem because it "
        "works from the NDC; the truncated name is an artifact of the PUBLIC "
        "release, and the NDC is not in it.")
    doc.add_paragraph(
        "IT BITES COMBINATION PRODUCTS. Of the failing claims, 67.4% are "
        "multi-ingredient, averaging 29.2 characters -- pressed against the "
        "ceiling. Combination products that SUCCEED average 26.4. Three "
        "ingredients plus salt qualifiers runs past 30 characters and the "
        "tail is lost. It has nothing to do with the drugs being rare: the "
        "failures average 2,789 claims each and reach 84,629.")
    doc.add_paragraph(
        "VACCINES ARE A MISSING SOURCE, NOT A BROKEN JOIN. Zero of 93,956 "
        "labels mention Shingrix. openFDA's drug-label endpoint is built from "
        "DailyMed's human prescription and OTC parts and drops the remainder "
        "archive, which is where vaccine labels live. RxNorm does carry their "
        "identity -- Shingrix resolves to RXCUI 1986826 -- so the gap is the "
        "label text, not the identification.")
    doc.add_paragraph(
        "AND THE PRESCRIBERS BEHIND IT ARE ONE PROFESSION. Of the providers "
        "with no coded drug at all, 349 of 469 are PHARMACISTS, and 716 of "
        "their 717 records are immunizations. Pharmacists appear as "
        "prescribers in this file because immunization is the authority they "
        "hold. So the vaccine gap is not scattered across medicine; it is one "
        "profession doing one thing the label source does not cover.")

    h(doc, "12A.3 The same result counted in DRUGS", 2)
    doc.add_paragraph(
        "Drugs is the harshest of the four denominators and the one an "
        "auditor will press on, so it is stated rather than left to be "
        "derived. A drug here is a distinct brand-and-generic pair as the "
        "file presents it.")
    table(doc,
          ["", "Drugs", "%", "Molecules", "Claims", "%", "Days supply", "%"],
          [["TOTAL IN FILE", "1,910", "100.00", "1,186", "33,534,542",
            "100.00", "1,725,195,125", "100.00"],
           ["with an ICD-10 code", "1,637", "85.71", "1,130", "32,781,256",
            "97.75", "1,703,608,152", "98.75"],
           ["WITHOUT an ICD-10 code", "273", "14.29", "56", "753,286",
            "2.25", "21,586,973", "1.25"]],
          [2.3, 0.8, 0.6, 1.0, 1.1, 0.6, 1.3, 0.6], size=7.8)
    doc.add_paragraph(
        "14.29% of the catalogue is 1.25% of the doses. The drugs that fail "
        "are small drugs -- which is the honest way to say that the headline "
        "is flattered by volume and the tail is worse than it looks.")

    h(doc, "12A.4 The drug recovery funnel, start to finish", 2)
    table(doc,
          ["", "Drugs", "Claims", "Days supply"],
          [["START -- coded before today", "1,391", "29,423,041",
            "1,537,393,715"],
           ["+ folded salt key, brand rung, device-suffix strip", "+246",
            "+3,358,215", "+166,214,437"],
           ["END -- coded today", "1,637", "32,781,256", "1,703,608,152"],
           ["gain", "+17.7%", "+11.4%", "+10.8%"]],
          [4.2, 1.3, 1.8, 2.0], size=8)
    doc.add_paragraph(
        "Three join defects, no new data source, nearly a fifth more of the "
        "catalogue. Every one of them was a key-format error rather than a "
        "missing fact: the labels were present the whole time.")

    h(doc, "12A.5 Who is behind the missing 1.25%", 2)
    table(doc,
          ["Cause", "Drugs", "NPIs", "Claims", "Days supply", "% days"],
          [["CMS generic truncated at 30 characters", "83", "5,335",
            "364,266", "12,372,661", "0.72"],
           ["not a drug -- device or supply", "73", "1,935", "95,594",
            "5,497,080", "0.32"],
           ["molecule found, no label", "61", "1,687", "89,226",
            "1,827,934", "0.11"],
           ["unknown to the vocabularies", "41", "1,253", "43,430",
            "1,556,256", "0.09"],
           ["vaccine or biologic", "15", "763", "160,770", "333,042",
            "0.02"],
           ["label in hand, coding pass incomplete", "14", "672", "25,461",
            "708,471", "0.04"],
           ["TOTAL", "273", "6,791", "753,286", "21,586,973", "1.25"]],
          [3.4, 0.8, 0.9, 1.2, 1.5, 0.8], size=7.8)
    doc.add_paragraph(
        "6,791 prescribers -- 36% of all of them -- are touched by the "
        "missing 1.25%. But only 436 have NOTHING coded at all. So for 6,355 "
        "providers this is a thin spot in an otherwise complete record, and "
        "for 436 it is an empty one. Those two cases are not the same product "
        "failure and must not be reported as one: a provider with no Medicare "
        "profile reads as a provider with no Medicare practice.")

    h(doc, "12A.6 A correction recorded against itself", 2)
    doc.add_paragraph(
        "An earlier draft of this analysis asserted a 99.05% ceiling. That "
        "figure was computed as 100% minus a floor, presented beside measured "
        "results, and rested on an unverified premise -- that 175 identified "
        "products would acquire labels from a source not yet shown to carry "
        "them. Most of those products turned out to have labels already, and "
        "the real defect was a brand suffix. The figure was never measured "
        "and is withdrawn. It is recorded here because a withdrawn number "
        "that leaves no trace is indistinguishable from one that was never "
        "wrong.")

    # ================================================================== 13
    h(doc, "13. Completeness across the three source domains", 1)
    doc.add_paragraph(
        "The port performs nesting only (BR-028), so every field it emits "
        "must already exist as a column carrying values. This is that "
        "inventory, MEASURED rather than declared, so the port can be "
        "straightforward instead of exploratory.")
    doc.add_paragraph(
        "WHAT BOUNDS THE TWO-STATE RISK. Two states cannot reveal a field "
        "populated only elsewhere. Three things bound that: the CMS Part D "
        "file is a FIXED 22-column layout identical for every state; the FDA "
        "labels, ICD-10-CM, CCSR and coverage articles are loaded NATIONALLY; "
        "and the provider collection's field set is our own, so there is no "
        "unknown-field risk in it at all. Delaware and Alabama were also "
        "chosen as opposite ends of the data-quality range, so a field empty "
        "in both is unlikely to matter anywhere.")

    h(doc, "13.1 FDA", 2)
    table(doc,
          ["Fact", "Measured"],
          [["label rows loaded (national, OTC excluded)", "281,838"],
           ["distinct set_id", "93,956"],
           ["carrying indication prose", "263,784 (93.6%)"],
           ["carrying an openfda product_type", "35,674 (38.0%)"],
           ["molecule-to-label links", "27,142 over 938 molecules"],
           ["molecules reached from the claim file", "1,186"],
           ["top-level fields the FDA publishes", "122"],
           ["fields originally loaded", "12 -- the other 110 discarded"],
           ["cohort sections now kept (BR-020)",
            "9, plus their table variants"],
           ["cohort rows after dedupe",
            "128,395, one per label, UNIQUE on set_id"],
           ["duplicate rows removed",
            "171,185 -- 2.33 cached download generations of the same "
            "14-file bulk set"],
           ["molecules carrying a cohort note", "937 of 938 (99.9%)"],
           ["molecules carrying a pediatric_use section", "910"]],
          [4.4, 4.9])
    doc.add_paragraph(
        "SPARSE BUT NOT EMPTY, and the distinction matters for the schema: "
        "active_ingredient 0.8%, product_type 37.7%, "
        "spl_unclassified_section 43.1%. Those fields exist and carry values "
        "for some labels; a schema that omitted them would be wrong, and one "
        "that marked them required would be too.")

    h(doc, "13.2 NUCC", 2)
    table(doc,
          ["Fact", "Measured"],
          [["vocabulary entries", "884"],
           ["distinct primary NUCC codes held by providers in scope", "726"],
           ["of those, ABSENT from the vocabulary", "ZERO"],
           ["providers with no primary NUCC", "ZERO of 137,040"],
           ["display name / grouping / classification / section", "100%"],
           ["definition", "99.1%"],
           ["specialization", "72.7%"],
           ["notes", "68.0%"],
           ["derived flags (can_prescribe, is_homeopathic, is_disqualified, "
            "is_supplemented)", "100%"]],
          [4.9, 4.4])
    doc.add_paragraph(
        "A PORT BLOCKER FOUND AND CLEARED. The vocabulary was held as a "
        "single JSONB blob. A nesting-only port cannot reach into JSON -- "
        "that is computation -- so the keys are surfaced as COLUMNS. "
        "Surfacing an existing key is mechanical and invents nothing; "
        "parsing one at port time would have been the invention.")

    h(doc, "13.3 NPI", 2)
    table(doc,
          ["Fact", "Measured"],
          [["providers", "137,173 rows, 137,173 distinct NPI"],
           ["BILLING addresses (BR-010)",
            "137,173 rows over 137,173 NPI -- 1:1, a valid anchor"],
           ["PRACTICE addresses (BR-011)",
            "137,113 NPI, worst 356 on one NPI -- display only"],
           ["licenses", "124,639 rows over 101,140 NPI"],
           ["taxonomies", "169,882 rows over 137,040 NPI"],
           ["distinct prescriber NPI in the claim file", "18,759"],
           ["matched a provider record, on NPI alone", "18,759 (100.00%)"],
           ["carrying a billing address", "18,759 (100.00%)"]],
          [4.0, 5.3])
    doc.add_paragraph(
        "A FIGURE THAT LOOKS LIKE A GAP AND IS NOT. The individual-name "
        "fields sit at 75% -- last name, first name, sex, sole-proprietor "
        "standing. The other 25% are INSTITUTIONS, which have no first name. "
        "A schema must therefore make those fields conditional on entity "
        "type rather than simply optional, or an institution with no first "
        "name reads as an incomplete record.")

    h(doc, "13.4 The one field that exists and is empty", 2)
    doc.add_paragraph(
        "Across every source table, exactly ONE column exists and carries no "
        "value in either state: provider_business_address.county_fips. That "
        "is the entire list. It is the measured answer to what the port will "
        "omit for want of VALUES rather than for want of EXISTENCE, and the "
        "schema must be able to tell those two apart.")

    h(doc, "13.5 The delivered collection", 2)
    table(doc,
          ["Fact", "Measured"],
          [["delivered rows", "16,242,836"],
           ["ProviderMedicare documents at the approved grain (BR-024)",
            "469,164"],
           ["providers", "18,081"],
           ["molecules", "937"],
           ["indications", "2,549"],
           ["ICD codes", "1,853"],
           ["columns 100% populated", "every one except the two below"],
           ["ccsr_category / body_system",
            "43.4% -- the CCSR grouping genuinely does not cover every code"],
           ["entity_type", "99.6%"]],
          [4.6, 4.7])

    # ================================================================== 14
    h(doc, "14. Two findings recorded rather than buried", 1)

    h(doc, "14.1 The indication vocabulary rung is a SUBSTRING MATCH", 2)
    doc.add_paragraph(
        "The rung supplying 3,995 indications across 837 molecules does not "
        "read a coded field off the label. The label carries no coded "
        "indication. The rung matches vocabulary terms as SUBSTRINGS into the "
        "label's lowercased indication prose, capped arbitrarily at 25 hits "
        "per molecule.")
    doc.add_paragraph(
        "It therefore CANNOT be reclassified as a coded join, however much "
        "less inference in the chain would be preferred. A substring match "
        "cannot distinguish 'indicated for X' from 'do not use in X'. It is "
        "also the same shape as two defects already found here: a containment "
        "match that once spanned 280 labels across 145 ingredients, and "
        "reading a relatedness crosswalk as an equivalence.")
    doc.add_paragraph(
        "WHAT THE EVIDENCE ACTUALLY SHOWS, against the expectation. An "
        "explicit negation test found 2 cases in 3,995. Only 15 of 837 "
        "molecules hit the cap. And the molecule with the longest list -- 25 "
        "indications including Stevens-Johnson syndrome, mycosis fungoides "
        "and nephrotic syndrome -- is methylprednisolone acetate, a "
        "corticosteroid whose label genuinely does list all of them. The rung "
        "was predicted to be producing wrong facts and the measurement did "
        "not support it. What survives is the methodological point and the "
        "confidence question, not a demonstrated error rate.")
    doc.add_paragraph(
        "THE OPEN QUESTION IT LEAVES. Under BR-003 this rung receives "
        "confidence 1.0, because its provenance is not inference. A substring "
        "match is not 'we know this'. Whether 1.0 is right for it is "
        "unresolved, and is recorded here rather than settled quietly.")

    h(doc, "14.2 On the drug path, inference is intrinsic", 2)
    doc.add_paragraph(
        "The FDA label states its indications as PROSE. There is no coded "
        "indication field in the record, anywhere. So reading an indication "
        "is an inference by the nature of the source rather than by a "
        "shortcut, and no reclassification changes that.")
    doc.add_paragraph(
        "THE ONE REAL LEVER IS A DIFFERENT PATH. Medicare's coverage articles "
        "link PROCEDURES to covered diagnoses, deterministically, across "
        "467,228 rows over 1,062 articles. This chain uses 168 of those rows "
        "-- 0.04% -- because it runs drug to indication to diagnosis, and the "
        "coverage source answers the PROCEDURE question. Bringing Part B 'by "
        "Provider and Service' into the chain is the way to move weight off "
        "inference. EPIC-010-F-007-S-002 already names Part B as a source; "
        "only the Part D half was built. That is a scope decision and it "
        "belongs to the operator.")

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
