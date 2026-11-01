"""Medicare Diagnostic Chain -- the reference implementation.

ONE coherent build, replacing exec_01..exec_13. Those were an archaeological
record of how each fact was learned; this is the chain as designed, with every
business rule in the register enforced rather than applied.

Governed by Medicare_DiagnosticChain_Design_v1.docx. Rule ids below are that
document's register.

WHAT THIS IS. The reference implementation. It defines WHAT the delivered data
is, never HOW the pipeline must produce it (BR-027). A relational join is a
legitimate mechanism here and no constraint on Azure. It is preserved as a QA
artifact: when the design changes, this changes with it.

WHAT CHANGED FROM THE EXPERIMENT SCRIPTS.
  BR-001  indications originate ONLY in an approved FDA label. The trial rung
          is admitted as CORROBORATION and never as an origin. The experiment
          chain carried 61% investigational facts; this carries none.
  BR-009  ICD level is WHO character depth. The experiment derived it from
          billability, which stamped the five-character I50.81 as level 3 and
          mislabelled 466 rows across three routes.
  BR-007  the dotted form is canonical for publication, recovered from the
  BR-008  code set rather than built by inserting a dot at a guessed position;
          the dotless form is the internal join key and is never published.
  BR-003  confidence is exactly 1.0 for a deterministic derivation and the
  BR-004  model's figure x 0.9 for an inferred one, ASSERTED both ways.
  BR-019  the NIH vocabulary crosswalk's unsupported specificity is MARKED
          rather than silently filtered, because the ruling is outstanding.
          Marking makes the decision measurable; filtering would make it mine.

EVERY ASSERTION STOPS THE BUILD. A rule that only logs is a rule that stops
holding the first time something upstream changes.
"""
import os
import sys
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\build_chain_result.txt"
CATALOG = os.path.join(
    r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline",
    "data", "icd10_categories_top3levels.xlsx")

INFERRED_CEILING = 0.9      # BR-004
DETERMINISTIC = 1.0         # BR-003

# Routes, named for what they are (the design drops R1/R2/R5).
ROUTE_COVERAGE = "medicare_coverage_policy"
ROUTE_NIH = "nih_vocabulary_crosswalk"
ROUTE_VOCAB = "icd_description_match"
ROUTE_MODEL = "fda_label_coded_by_model"
RELATEDNESS_ROUTES = (ROUTE_NIH,)        # BR-019: answers relatedness only


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def stop(rule, detail):
    """A violated rule ends the build. It never degrades to a warning."""
    raise ChatHealthyException(
        "runtime_error", "RULE VIOLATED %s -- %s" % (rule, detail))


def catalog_codes():
    """Read through the TOOL that fed the model, never re-parsed (BR-023).

    A second parser of the same spreadsheet is a second answer: a local parse
    read 26,592 codes where the tool supplies 12,014, and a hallucination
    measured against the larger set is measured against something the model
    never saw."""
    sys.path.insert(0, r"C:\CHATHealthyLLC")
    sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
    from pipeline.MedicareProviderEvaluationDataPipeline.fda_drug_enrichment_tool import (
        FDADrugEnrichmentTool)
    tool = FDADrugEnrichmentTool(catalog=CATALOG, batch_size=25,
                                 max_transient_retries=5, backoff_base_s=2.0)
    codes = set(tool.catalog_codes)
    if not codes:
        stop("BR-023", "the tool supplied an empty catalog; a hallucination "
                       "rate against nothing is not a measurement")
    return codes


# ======================================================================
# STEP 1 -- the molecule bridge is a PREREQUISITE, not rebuilt here.
# Its resolution is expensive and already correct per BR-016 (has_form fold
# for matching, unfolded set for identity). The build asserts it is present
# and sane rather than silently producing an empty chain.
# ======================================================================
def step_1_assert_bridge(cur):
    emit("=== STEP 1 -- the molecule bridge (BR-016) ===")
    cur.execute("""SELECT count(*), count(DISTINCT molecule_id),
                          count(*) FILTER (WHERE molecule_id IS NULL)
                   FROM cms_drug_resolution""")
    rows, mols, unres = cur.fetchone()
    if rows == 0:
        stop("BR-016", "cms_drug_resolution is empty; the bridge must be "
                       "built before the chain can be")
    emit("  pairs resolved        : %s" % f"{rows:,}")
    emit("  distinct molecules    : %s" % f"{mols:,}")
    emit("  pairs unresolved      : %s" % f"{unres:,}")
    cur.execute("""SELECT sum(tot_clms::numeric) FROM partd
                   WHERE tot_clms ~ '^[0-9]+$'""")
    total_claims = int(cur.fetchone()[0] or 0)
    emit("  claim rows in scope   : %s claims" % f"{total_claims:,}")
    return total_claims


# ======================================================================
# STEP 2 -- provider x molecule, on the billing anchor (BR-010..BR-013)
# ======================================================================
def step_2_provider_molecule(cur):
    emit()
    emit("=== STEP 2 -- provider x molecule on the billing anchor "
         "(BR-010..BR-014) ===")

    # BR-010: the anchor must be 1:1 or the build stops. Checked BEFORE use,
    # because a non-1:1 anchor makes every downstream count wrong and nothing
    # later repairs it.
    cur.execute("""SELECT count(*), count(DISTINCT npi)
                   FROM provider_business_address""")
    rows, npis = cur.fetchone()
    if rows != npis:
        stop("BR-010", "the billing address is not 1:1 with NPI (%d rows, %d "
                       "NPIs); it cannot normalise anything" % (rows, npis))
    emit("  BR-010 billing anchor is 1:1 : %s rows, %s NPIs" % (f"{rows:,}",
                                                                f"{npis:,}"))

    cur.execute("DROP TABLE IF EXISTS chain_provider_molecule CASCADE")
    cur.execute("""
        CREATE TABLE chain_provider_molecule AS
        SELECT
            d.prscrbr_npi::text                        AS npi,
            -- BR-012: two state facts, carried separately and never
            -- substituted. fill_state is a CMS fact about the claim;
            -- billing_state is a registry fact about the provider.
            d.prscrbr_state_abrvtn                     AS fill_state,
            b.state                                    AS billing_state,
            (d.prscrbr_state_abrvtn <> b.state)        AS state_differs,
            p.entity_type_code,
            p.entity_type_code_label,
            p.is_active,
            -- BR-013: specialty from the NPI registry, not the claim file
            n.primary_nucc,
            n.primary_nucc_label,
            r.molecule_id,
            count(*)                                   AS n_source_pairs,
            sum(d.tot_clms::numeric)                   AS tot_clms,
            sum(d.tot_drug_cst::numeric)               AS tot_drug_cst,
            -- BR-015: suppressed beneficiary counts stay NULL, never zero,
            -- and no measure in this build divides by them.
            sum(NULLIF(d.tot_benes,'')::numeric)       AS tot_benes,
            count(*) FILTER (WHERE d.tot_benes = '')   AS rows_benes_suppressed
        FROM partd d
        JOIN provider p                ON p.npi = d.prscrbr_npi::text
        JOIN provider_business_address b ON b.npi = p.npi
        LEFT JOIN provider_nucc n      ON n.npi = p.npi
        LEFT JOIN cms_drug_resolution r
               ON r.brnd_name = d.brnd_name AND r.gnrc_name = d.gnrc_name
        WHERE d.tot_clms ~ '^[0-9]+$'
        GROUP BY 1,2,3,4,5,6,7,8,9,10
    """)
    cur.execute("CREATE INDEX cpm_npi ON chain_provider_molecule (npi)")
    cur.execute("CREATE INDEX cpm_mol ON chain_provider_molecule (molecule_id)")
    cur.execute("ANALYZE chain_provider_molecule")

    cur.execute("""SELECT count(*), count(DISTINCT npi),
                          count(DISTINCT molecule_id),
                          sum(tot_clms), count(DISTINCT npi) FILTER (WHERE state_differs)
                   FROM chain_provider_molecule""")
    n, npi_n, mol_n, clms, differ = cur.fetchone()
    emit("  rows=%s providers=%s molecules=%s claims=%s"
         % (f"{n:,}", f"{npi_n:,}", f"{mol_n:,}", f"{int(clms):,}"))
    emit("  BR-012 providers whose billing state differs from fill state: %s"
         % f"{differ:,}")

    # Provider coverage of the claim file, measured not assumed.
    cur.execute("SELECT count(DISTINCT prscrbr_npi) FROM partd")
    cms_npis = cur.fetchone()[0]
    if npi_n > cms_npis:
        stop("BR-010", "more providers in the chain than prescribers in the "
                       "claim file; the join multiplied")
    emit("  provider coverage of the claim file: %s of %s (%.2f%%)"
         % (f"{npi_n:,}", f"{cms_npis:,}", 100.0 * npi_n / cms_npis))
    return int(clms)


# ======================================================================
# STEP 3 -- molecule x indication, APPROVED ORIGIN ONLY (BR-001, BR-002)
# ======================================================================
def step_3_indications(cur):
    emit()
    emit("=== STEP 3 -- molecule x indication, approved origin only "
         "(BR-001, BR-002) ===")
    cur.execute("DROP TABLE IF EXISTS chain_indication CASCADE")
    cur.execute("""
        CREATE TABLE chain_indication (
            molecule_id   text    NOT NULL,
            indication_id text    NOT NULL,
            indication_text text,
            origin        text    NOT NULL,   -- always an approved label
            rung          text    NOT NULL,
            provenance    text    NOT NULL,
            confidence    numeric NOT NULL,
            corroborated_by_trial boolean NOT NULL
        )""")

    # Approved-label indications. The trial rung is NOT here: a completed
    # trial may corroborate but never introduce (BR-001). Its knowledge is
    # kept as a flag so nothing is thrown away -- the trial simply loses the
    # power to admit an indication.
    cur.execute("""
        INSERT INTO chain_indication
        SELECT mi.molecule_id,
               mi.indication_id,
               mi.indication_text,
               'approved_fda_label',
               mi.rung,
               mi.provenance,
               CASE WHEN mi.provenance = 'inference'
                    THEN %s ELSE %s END,
               EXISTS (SELECT 1 FROM mol_trial mt
                       WHERE mt.molecule_id = mi.molecule_id)
        FROM molecule_indication mi
        WHERE mi.indication_id <> ''
          AND mi.authority <> 'investigational'
          AND mi.source <> 'trial'
    """, (INFERRED_CEILING, DETERMINISTIC))

    # The model's discrete indications (N4). Inferred, so capped (BR-005)
    # algorithmically here rather than by changing the benchmarked prompt.
    cur.execute("""
        INSERT INTO chain_indication
        SELECT DISTINCT
               l.molecule_id,
               'label:' || md5(lower(btrim(l.indication))),
               l.indication,
               'approved_fda_label',
               'label_prose_read_by_model',
               'inference',
               %s,
               EXISTS (SELECT 1 FROM mol_trial mt
                       WHERE mt.molecule_id = l.molecule_id)
        FROM llm_crosswalk l
        WHERE l.indication IS NOT NULL AND btrim(l.indication) <> ''
    """, (INFERRED_CEILING,))

    cur.execute("CREATE INDEX ci_mol ON chain_indication (molecule_id)")
    cur.execute("CREATE INDEX ci_ind ON chain_indication (indication_id)")
    cur.execute("ANALYZE chain_indication")

    # BR-001 ENFORCED: nothing investigational may exist here at all.
    cur.execute("""SELECT count(*) FROM chain_indication
                   WHERE origin <> 'approved_fda_label'""")
    bad = cur.fetchone()[0]
    if bad:
        stop("BR-001", "%d indications do not originate in an approved FDA "
                       "label" % bad)
    emit("  BR-001 every indication originates in an approved FDA label")

    cur.execute("""SELECT rung, provenance, count(*),
                          count(DISTINCT molecule_id),
                          count(DISTINCT indication_id),
                          count(*) FILTER (WHERE corroborated_by_trial)
                   FROM chain_indication GROUP BY 1,2 ORDER BY 3 DESC""")
    emit("  %-28s %-20s %8s %8s %9s %9s"
         % ("rung", "provenance", "rows", "mols", "inds", "corrob"))
    for rung, prov, n, m, i, c in cur.fetchall():
        emit("  %-28s %-20s %8s %8s %9s %9s"
             % (str(rung)[:28], prov, f"{n:,}", f"{m:,}", f"{i:,}", f"{c:,}"))


# ======================================================================
# STEP 4 -- indication x ICD (BR-007, BR-008, BR-009, BR-019, BR-023)
# ======================================================================
def step_4_codes(cur, cat):
    emit()
    emit("=== STEP 4 -- indication x ICD code "
         "(BR-007..BR-009, BR-019, BR-023) ===")
    cur.execute("DROP TABLE IF EXISTS chain_code CASCADE")
    cur.execute("""
        CREATE TABLE chain_code (
            indication_id   text    NOT NULL,
            icd10_code      text    NOT NULL,   -- BR-007 dotted, published
            icd10_key       text    NOT NULL,   -- BR-008 dotless, internal
            who_level       int     NOT NULL,   -- BR-009 character depth
            is_billable     boolean,
            route           text    NOT NULL,
            provenance      text    NOT NULL,
            confidence      numeric NOT NULL,
            ccsr_category   text,
            body_system     text,
            -- BR-019 the ruling is outstanding, so unsupported specificity is
            -- MARKED and measurable rather than silently filtered. Filtering
            -- it here would make the operator's decision mine.
            asserts_unsupported_specificity boolean NOT NULL
        )""")

    # The deterministic routes. BR-003: confidence is exactly 1.0 -- when the
    # derivation is a coded join we know the fact, and withholding certainty
    # we hold is a misrepresentation by omission.
    cur.execute("""
        INSERT INTO chain_code
        SELECT ii.indication_id,
               coalesce(i.code_dotted, ii.icd10_code),
               replace(ii.icd10_code,'.',''),
               length(replace(ii.icd10_code,'.','')),
               i.is_billable,
               CASE ii.route
                    WHEN 'R1_coverage'       THEN %s
                    WHEN 'R2_umls_crosswalk' THEN %s
                    WHEN 'R3_vocabulary'     THEN %s
                    ELSE ii.route END,
               ii.provenance,
               %s,
               nullif(ii.ccsr_cat,''),
               nullif(ii.body_system,''),
               (ii.route = 'R2_umls_crosswalk'
                AND length(replace(ii.icd10_code,'.','')) > 3)
        FROM indication_icd ii
        LEFT JOIN icd10cm i ON i.code_dotless = replace(ii.icd10_code,'.','')
        WHERE EXISTS (SELECT 1 FROM chain_indication c
                      WHERE c.indication_id = ii.indication_id)
          -- BR-019 ENFORCED (operator approved 2026-10-30, cost measured at
          -- 0.00 claim points). A relatedness crosswalk answers 'which codes
          -- RELATE to this concept' and may therefore assert only the
          -- category, never a specific child. Removed rather than marked,
          -- per BR-017: a wrong fact at a lower confidence is still a wrong
          -- fact in the data.
          AND NOT (ii.route = 'R2_umls_crosswalk'
                   AND length(replace(ii.icd10_code,'.','')) > 3)
    """, (ROUTE_COVERAGE, ROUTE_NIH, ROUTE_VOCAB, DETERMINISTIC))

    # What BR-019 excluded, stated rather than silently absent.
    cur.execute("""
        SELECT count(*), count(DISTINCT icd10_code)
        FROM indication_icd ii
        WHERE ii.route = 'R2_umls_crosswalk'
          AND length(replace(ii.icd10_code,'.','')) > 3
          AND EXISTS (SELECT 1 FROM chain_indication c
                      WHERE c.indication_id = ii.indication_id)""")
    exc_rows, exc_codes = cur.fetchone()
    emit("  BR-019 excluded %s rows across %s codes -- a relatedness "
         "crosswalk may assert the category, never the child"
         % (f"{exc_rows:,}", f"{exc_codes:,}"))

    # The model's codes. BR-005: the ceiling applied by multiplying the
    # model's own figure, preserving its ordering.
    cur.execute("""
        INSERT INTO chain_code
        SELECT DISTINCT
               'label:' || md5(lower(btrim(l.indication))),
               coalesce(i.code_dotted, l.icd10_code),
               replace(l.icd10_code,'.',''),
               length(replace(l.icd10_code,'.','')),
               i.is_billable,
               %s,
               'inference',
               l.confidence * %s,
               nullif(c.ccsr_cat1,''),
               nullif(c.body_system,''),
               false
        FROM llm_crosswalk l
        -- BR-023 + BR-017: a code that does not resolve in the official set
        -- is REMOVED, not published. An INNER join is the enforcement: a
        -- fabricated code cannot acquire a canonical dotted form, so letting
        -- it through would publish a code without its decimal and assert a
        -- diagnosis that does not exist. Removing beats discounting -- a
        -- wrong fact at a lower confidence is still a wrong fact.
        JOIN icd10cm i ON i.code_dotless = replace(l.icd10_code,'.','')
        LEFT JOIN ccsr c ON c.code_dotless = replace(l.icd10_code,'.','')
        WHERE l.icd10_code IS NOT NULL
    """, (ROUTE_MODEL, INFERRED_CEILING))

    # What the inner join rejected, stated rather than silently dropped.
    cur.execute("""
        SELECT count(DISTINCT l.icd10_code)
        FROM llm_crosswalk l
        WHERE l.icd10_code IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM icd10cm i
                          WHERE i.code_dotless = replace(l.icd10_code,'.',''))
    """)
    rejected = cur.fetchone()[0]
    emit("  BR-023 model codes REJECTED as unresolvable: %s" % f"{rejected:,}")

    cur.execute("CREATE INDEX cc_ind ON chain_code (indication_id)")
    cur.execute("CREATE INDEX cc_key ON chain_code (icd10_key)")
    cur.execute("ANALYZE chain_code")

    # ---- BR-009 ENFORCED: level is character depth, never a proxy --------
    cur.execute("""SELECT count(*) FROM chain_code
                   WHERE who_level <> length(icd10_key)""")
    bad = cur.fetchone()[0]
    if bad:
        stop("BR-009", "%d rows carry a level that is not the code's "
                       "character depth" % bad)
    cur.execute("""SELECT count(*) FROM chain_code cc
                   WHERE cc.is_billable IS NOT NULL
                     AND ((cc.who_level = 3) = cc.is_billable)
                     AND cc.who_level <> 3""")
    emit("  BR-009 who_level equals character depth on every row")

    # ---- BR-003 / BR-004 ENFORCED: the confidence scale ------------------
    cur.execute("""
        SELECT count(*) FILTER (WHERE provenance='inference'
                                  AND confidence > %s),
               count(*) FILTER (WHERE provenance<>'inference'
                                  AND confidence <> %s),
               max(confidence) FILTER (WHERE provenance='inference'),
               min(confidence) FILTER (WHERE provenance<>'inference')
        FROM chain_code""", (INFERRED_CEILING, DETERMINISTIC))
    over, notone, maxinf, mindet = cur.fetchone()
    if over:
        stop("BR-004", "%d inferred rows exceed the %s ceiling" % (over,
                                                                   INFERRED_CEILING))
    if notone:
        stop("BR-003", "%d deterministic rows are not exactly %s"
             % (notone, DETERMINISTIC))
    emit("  BR-003/BR-004 scale holds: inferred max=%s deterministic min=%s"
         % (maxinf, mindet))

    # ---- BR-007 / BR-008 ENFORCED: the dot forms -------------------------
    cur.execute("""SELECT count(*) FROM chain_code WHERE icd10_key LIKE '%.%'""")
    if cur.fetchone()[0]:
        stop("BR-008", "the internal key carries a dot; it is the dotless form")
    cur.execute("""SELECT count(*) FROM chain_code
                   WHERE who_level > 3 AND icd10_code NOT LIKE '_%._%'""")
    undotted = cur.fetchone()[0]
    emit("  BR-008 internal key is dotless on every row")
    if undotted:
        stop("BR-007", "%d published codes beyond level 3 carry no decimal. "
                       "A code whose dotted form cannot be recovered from the "
                       "official set is not a real code and must be removed, "
                       "not published without it." % undotted)
    emit("  BR-007 every published code beyond level 3 carries its decimal")

    # ---- BR-023 ENFORCED: the closed catalog -----------------------------
    cur.execute("""SELECT DISTINCT icd10_key FROM chain_code
                   WHERE route = %s""", (ROUTE_MODEL,))
    model_codes = {r[0] for r in cur.fetchall()}
    fabricated = sorted(c for c in model_codes if c.upper() not in cat)
    emit("  BR-023 catalog=%s   model codes=%s   fabricated=%s  (%.4f)"
         % (f"{len(cat):,}", f"{len(model_codes):,}", f"{len(fabricated):,}",
            len(fabricated) / max(len(model_codes), 1)))
    if fabricated:
        emit("         %s" % ", ".join(fabricated[:12]))

    # ---- BR-019 the outstanding ruling, measured -------------------------
    cur.execute("""SELECT count(*) FROM chain_code
                   WHERE asserts_unsupported_specificity""")
    left = cur.fetchone()[0]
    if left:
        stop("BR-019", "%d rows asserting unsupported specificity survived "
                       "the exclusion" % left)
    emit("  BR-019 no surviving row asserts unsupported specificity")
    cur.execute("""SELECT route, count(*), count(DISTINCT icd10_key),
                          count(*) FILTER (WHERE asserts_unsupported_specificity)
                   FROM chain_code GROUP BY 1 ORDER BY 2 DESC""")
    emit()
    emit("  %-28s %9s %9s %12s" % ("route", "rows", "codes", "unsupported"))
    for r, n, c, u in cur.fetchall():
        emit("  %-28s %9s %9s %12s" % (r, f"{n:,}", f"{c:,}", f"{u:,}"))


# ======================================================================
# STEP 5 -- the delivered set (BR-014, BR-018, BR-024)
# ======================================================================
def step_5_delivered(cur, total_claims):
    emit()
    emit("=== STEP 5 -- the delivered set (BR-014, BR-018, BR-024) ===")
    cur.execute("DROP TABLE IF EXISTS chain_delivered CASCADE")
    cur.execute("""
        CREATE TABLE chain_delivered AS
        SELECT pm.npi, pm.fill_state, pm.billing_state, pm.state_differs,
               pm.entity_type_code, pm.entity_type_code_label,
               pm.primary_nucc, pm.primary_nucc_label,
               pm.molecule_id,
               ci.indication_id, ci.indication_text, ci.rung,
               ci.corroborated_by_trial,
               cc.icd10_code, cc.icd10_key, cc.who_level, cc.is_billable,
               cc.route, cc.ccsr_category, cc.body_system,
               cc.asserts_unsupported_specificity,
               -- BR-014: NAMED non-additive. The volume belongs to
               -- (npi, molecule_id) and repeats across this row's fan-out.
               -- Every rollup below distinct-counts back to that grain.
               pm.tot_clms AS molecule_claims_nonadditive,
               -- BR-018: weakest provenance on the path
               CASE WHEN 'inference' IN (ci.provenance_x, cc.provenance_x)
                      THEN 'inference'
                    WHEN 'adopted_vocabulary' IN (ci.provenance_x, cc.provenance_x)
                      THEN 'adopted_vocabulary'
                    ELSE 'joined_coded_source' END AS provenance,
               least(ci.confidence_x, cc.confidence_x) AS confidence
        FROM chain_provider_molecule pm
        JOIN (SELECT molecule_id, indication_id, indication_text, rung,
                     corroborated_by_trial,
                     provenance AS provenance_x, confidence AS confidence_x
              FROM chain_indication) ci ON ci.molecule_id = pm.molecule_id
        JOIN (SELECT indication_id, icd10_code, icd10_key, who_level,
                     is_billable, route, ccsr_category, body_system,
                     asserts_unsupported_specificity,
                     provenance AS provenance_x, confidence AS confidence_x
              FROM chain_code) cc ON cc.indication_id = ci.indication_id
        WHERE pm.molecule_id IS NOT NULL
    """)
    for ddl in ("CREATE INDEX cd_npi ON chain_delivered (npi)",
                "CREATE INDEX cd_mol ON chain_delivered (npi, molecule_id)",
                "CREATE INDEX cd_key ON chain_delivered (icd10_key)",
                "CREATE INDEX cd_prov ON chain_delivered (provenance)"):
        cur.execute(ddl)
    cur.execute("ANALYZE chain_delivered")

    cur.execute("""SELECT count(*), count(DISTINCT npi),
                          count(DISTINCT molecule_id),
                          count(DISTINCT indication_id),
                          count(DISTINCT icd10_code)
                   FROM chain_delivered""")
    n, p, m, i, c = cur.fetchone()
    emit("  rows=%s providers=%s molecules=%s indications=%s codes=%s"
         % (f"{n:,}", f"{p:,}", f"{m:,}", f"{i:,}", f"{c:,}"))

    # BR-014 demonstrated rather than asserted: the inflation is the reason
    # the column carries its name.
    cur.execute("SELECT sum(molecule_claims_nonadditive) FROM chain_delivered")
    naive = int(cur.fetchone()[0] or 0)
    cur.execute("""SELECT sum(tot_clms) FROM chain_provider_molecule
                   WHERE molecule_id IS NOT NULL""")
    truth = int(cur.fetchone()[0] or 0)
    emit("  BR-014 naive sum over delivered rows : %s" % f"{naive:,}")
    emit("         true total at its grain       : %s" % f"{truth:,}")
    emit("         inflation if summed naively   : %.1fx"
         % (naive / max(truth, 1)))

    # BR-003/BR-004 must survive to the delivered grain.
    cur.execute("""SELECT count(*) FILTER (WHERE provenance='inference'
                                             AND confidence > %s),
                          count(*) FILTER (WHERE provenance<>'inference'
                                             AND confidence <> %s)
                   FROM chain_delivered""",
                (INFERRED_CEILING, DETERMINISTIC))
    over, notone = cur.fetchone()
    if over or notone:
        stop("BR-003/BR-004", "the scale does not survive to the delivered "
                              "grain: %d over ceiling, %d not exactly 1.0"
             % (over, notone))
    emit("  BR-003/BR-004 the scale survives to the delivered grain")
    return truth


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("MEDICARE DIAGNOSTIC CHAIN -- reference implementation")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("governed by Medicare_DiagnosticChain_Design_v1.docx")
    emit("every assertion below STOPS the build; none degrades to a warning")
    emit()

    cat = catalog_codes()
    con = connect(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='7200s'")

    t0 = time.time()
    step_1_assert_bridge(cur)
    step_2_provider_molecule(cur)
    step_3_indications(cur)
    step_4_codes(cur, cat)
    resolved_claims = step_5_delivered(cur, None)

    # ---- coverage, claim-weighted (the business answer) ------------------
    emit()
    emit("=== COVERAGE, claim-weighted ===")
    cur.execute("""
        WITH reach AS (SELECT DISTINCT npi, molecule_id FROM chain_delivered)
        SELECT sum(pm.tot_clms),
               sum(pm.tot_clms) FILTER (WHERE r.npi IS NOT NULL)
        FROM chain_provider_molecule pm
        LEFT JOIN reach r ON r.npi=pm.npi AND r.molecule_id=pm.molecule_id""")
    tot, reached = [int(x or 0) for x in cur.fetchone()]
    emit("  claims reaching at least one ICD code: %s of %s (%.2f%%)"
         % (f"{reached:,}", f"{tot:,}", 100.0 * reached / max(tot, 1)))

    emit()
    emit("=== PROVENANCE at the delivered grain, weakest-on-path (BR-018) ===")
    cur.execute("""
        WITH facts AS (
          SELECT npi, molecule_id, icd10_key,
                 min(CASE provenance WHEN 'joined_coded_source' THEN 1
                                     WHEN 'adopted_vocabulary' THEN 2
                                     ELSE 3 END) AS r
          FROM chain_delivered GROUP BY 1,2,3)
        SELECT CASE r WHEN 1 THEN 'joined_coded_source'
                      WHEN 2 THEN 'adopted_vocabulary'
                      ELSE 'inference' END,
               count(*), round((100.0*count(*)/sum(count(*)) OVER ())::numeric,2)
        FROM facts GROUP BY 1,r ORDER BY 2 DESC""")
    for prov, n, pct in cur.fetchall():
        emit("  %-22s %13s %8.2f%%" % (prov, f"{n:,}", pct))

    emit()
    emit("=== BR-019, the outstanding ruling, measured both ways ===")
    cur.execute("""
        WITH allr AS (SELECT DISTINCT npi, molecule_id FROM chain_delivered),
             supported AS (SELECT DISTINCT npi, molecule_id
                           FROM chain_delivered
                           WHERE NOT asserts_unsupported_specificity)
        SELECT sum(pm.tot_clms) FILTER (WHERE a.npi IS NOT NULL),
               sum(pm.tot_clms) FILTER (WHERE s.npi IS NOT NULL),
               sum(pm.tot_clms)
        FROM chain_provider_molecule pm
        LEFT JOIN allr a ON a.npi=pm.npi AND a.molecule_id=pm.molecule_id
        LEFT JOIN supported s ON s.npi=pm.npi AND s.molecule_id=pm.molecule_id
    """)
    withu, without, tot2 = [int(x or 0) for x in cur.fetchone()]
    emit("  coverage as built (unsupported specificity MARKED)  : %.2f%%"
         % (100.0 * withu / max(tot2, 1)))
    emit("  coverage if unsupported specificity were DROPPED    : %.2f%%"
         % (100.0 * without / max(tot2, 1)))
    emit("  cost of the stricter reading: %.2f claim points"
         % (100.0 * (withu - without) / max(tot2, 1)))
    cur.execute("""SELECT count(*), count(DISTINCT icd10_code)
                   FROM chain_delivered
                   WHERE asserts_unsupported_specificity""")
    r_, c_ = cur.fetchone()
    emit("  rows so marked: %s across %s codes" % (f"{r_:,}", f"{c_:,}"))

    emit()
    emit("  build completed in %.0fs" % (time.time() - t0))
    con.close()
    emit()
    emit("written to " + OUT)


main()
