"""Execution step 12 -- rebuild T3, T4, T5 on the corrected T2, and measure
the chain end to end against the benchmark.

THREE things change from exec_06 / exec_08:

1. T2 is now provider_molecule_v2 -- built on the BILLING address, which is the
   only 1:1 key to an NPI, asserted 1:1 before use. It carries fill_state and
   billing_state as SEPARATE facts (never substituted for one another) and NUCC
   from the NPI registry rather than the claim file's Prscrbr_Type.

2. R5 is now real. The N4+R5 inference ran through the benchmarked
   FDADrugEnrichmentTool with PROMPT 2 verbatim and gpt-5.6-luna, and its
   output is in llm_crosswalk. exec_08 had no R5 at all, so the chain's
   indication->ICD coverage was whatever the deterministic routes reached.

3. THE FAN-OUT OVER-COUNT IS FIXED. exec_08's t5_delivered carried pm.tot_clms
   on EVERY row of a 33x fan-out: one provider-molecule with three indications
   at four codes each wrote the full claim count twelve times. SUM(tot_clms)
   over that table is inflated by the fan-out factor, which is why I50 and
   I50.81 reported identical volume -- each inherited the whole figure.

   The per-row value was not wrong. The column invited a sum that was, and
   nothing in the table said so.

   The fix is NOT a fractional allocation. No requirement states a weighting
   rule, and inventing one would put an unapproved rule in the data where it
   would be indistinguishable afterwards from a specified one. Instead:

     - the volume column is NAMED non-additive, so a sum over it is visibly
       wrong rather than quietly wrong
     - every rollup reaches the volume by DISTINCT-counting back to
       (npi, molecule_id), the grain at which the volume is actually additive
     - a molecule reaching one code through two indications counts ONCE

Nothing here is inference. T5 is joins and distinct-counted sums only.
"""
import hashlib
import os
import sys
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\exec_12_chain_result.txt"
CATALOG = os.path.join(
    r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline",
    "data", "icd10_categories_top3levels.xlsx")

# the benchmark's figures, the only yardstick this step is measured against
BENCH_COVERAGE = 97.7
BENCH_HALLUCINATION = 0.000


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def catalog_codes():
    """The codes the model was actually given -- read through the TOOL, not
    re-parsed here.

    A second parser of the same spreadsheet is a second answer: mine read
    26,592 codes where the tool supplies 12,014, so a hallucination measured
    against mine was measured against a set the model never saw, and a more
    permissive one. The catalog has one reader and it is the one that fed the
    model."""
    import sys
    sys.path.insert(0, r"C:\CHATHealthyLLC")
    sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
    from pipeline.MedicareProviderEvaluationDataPipeline.fda_drug_enrichment_tool import (
        FDADrugEnrichmentTool)
    tool = FDADrugEnrichmentTool(catalog=CATALOG, batch_size=25,
                                 max_transient_retries=5, backoff_base_s=2.0)
    codes = set(tool.catalog_codes)
    if not codes:
        raise ChatHealthyException(
            "runtime_error",
            "the tool supplied an empty catalog; refusing to "
            "report a hallucination rate against nothing")
    return codes


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("EXECUTION STEP 12 -- rebuild T3/T4/T5 on the corrected T2")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("yardstick: the EPIC-010-F-007-S-008 benchmark, 97.7% after "
         "retest-on-drop, hallucination 0.000")
    emit()

    con = connect(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='7200s'")

    # ---- gate: never build on a partial inference ------------------------
    cur.execute("SELECT count(DISTINCT molecule_id) FROM llm_crosswalk")
    got = cur.fetchone()[0]
    cur.execute("""
        SELECT count(*) FROM (
          SELECT ml.molecule_id
          FROM mol_label ml
          JOIN (SELECT DISTINCT set_id, indications_and_usage FROM fda_label) l
            ON l.set_id = ml.set_id
          GROUP BY 1
          HAVING string_agg(DISTINCT nullif(l.indications_and_usage,''), ' ') <> ''
        ) x""")
    want = cur.fetchone()[0]
    emit("=== GATE: is the inference complete? ===")
    emit("  molecules with prose to crosswalk : %s" % f"{want:,}")
    emit("  molecules present in llm_crosswalk: %s" % f"{got:,}")
    if got < want:
        emit("  INCOMPLETE -- %s still to come. REFUSING to build the chain on "
             "a partial inference, because every coverage figure would be "
             "understated by an amount that looks like a result."
             % f"{want-got:,}")
        raise ChatHealthyException(
            "runtime_error",
            "inference incomplete; rerun when exec_09 finishes")
    emit("  complete; proceeding")
    emit()

    # ================================================================== T3
    emit("=== T3 molecule -> indication, rebuilt with the N4 output ===")
    cur.execute("""
        DROP TABLE IF EXISTS molecule_indication_v2;
        CREATE TABLE molecule_indication_v2 (
          molecule_id text, indication_id text, indication_text text,
          source text, authority text, rung text, provenance text);

        -- the deterministic rows, carried forward unchanged
        INSERT INTO molecule_indication_v2
        SELECT molecule_id, indication_id, indication_text, source, authority,
               rung, provenance
        FROM molecule_indication
        WHERE indication_id <> '';

        -- NEW: N4, the discrete indications the sanctioned prompt extracted
        INSERT INTO molecule_indication_v2
        SELECT DISTINCT
               molecule_id,
               'llm:' || md5(lower(btrim(indication))),
               indication,
               'fda_label_prose',
               'approved_label',
               'n4_prompt2',
               'inference'
        FROM llm_crosswalk
        WHERE indication IS NOT NULL AND btrim(indication) <> '';

        CREATE INDEX mi2_mol ON molecule_indication_v2 (molecule_id);
        CREATE INDEX mi2_ind ON molecule_indication_v2 (indication_id);
        ANALYZE molecule_indication_v2;
    """)
    cur.execute("""SELECT rung, count(*), count(DISTINCT molecule_id),
                          count(DISTINCT indication_id)
                   FROM molecule_indication_v2 GROUP BY 1 ORDER BY 2 DESC""")
    emit("  %-16s %10s %11s %13s" % ("rung", "rows", "molecules", "indications"))
    for rung, n, m, i_ in cur.fetchall():
        emit("  %-16s %10s %11s %13s"
             % (str(rung)[:16], f"{n:,}", f"{m:,}", f"{i_:,}"))

    # ================================================================== T4
    emit()
    emit("=== T4 indication -> ICD, all five routes ===")
    cur.execute("""
        DROP TABLE IF EXISTS indication_icd_v2;
        CREATE TABLE indication_icd_v2 (
          indication_id text, icd10_code text, icd10_code_dotless text,
          level int, is_billable bool,
          route text, provenance text, ccsr_cat text, body_system text,
          confidence double precision);

        -- R1-R4. The routes disagree on dot format: coverage and UMLS emit
        -- DOTTED, the model emits DOTLESS. Both forms are carried, the dotted
        -- one recovered from icd10cm rather than built by inserting a dot at a
        -- guessed position, so every route becomes joinable on ONE key and two
        -- routes reaching the same code actually deduplicate.
        -- CONFIDENCE SCALE, operator's rule 2026-10-30:
        --   1.0        reserved for a deterministic derivation. The only way
        --              such a fact is wrong is a bug, and bug risk is managed
        --              by the gates, not expressed as a discount. Withholding
        --              certainty we actually hold is a misrepresentation by
        --              omission, so a coded join states 1.0.
        --   0 .. 0.9   anything inferred. Nothing inferred ever reaches 1.0.
        -- The cap is applied ALGORITHMICALLY here, after the model returns, by
        -- multiplying its figure by 0.9. It is NOT done by telling the model a
        -- new scale: PROMPT 2 is verbatim from the benchmark and changing its
        -- output contract would invalidate luna's measured scores until
        -- re-benchmarked. Multiplying also preserves the model's ordering --
        -- 55.4% of its codes come back at or above 0.9, so a clamp would
        -- collapse the majority of the set onto one value.
        INSERT INTO indication_icd_v2
        SELECT ii.indication_id,
               coalesce(i.code_dotted, ii.icd10_code),
               replace(ii.icd10_code,'.',''),
               ii.level, ii.is_billable, ii.route,
               ii.provenance, ii.ccsr_cat, ii.body_system,
               1.0
        FROM indication_icd ii
        LEFT JOIN icd10cm i ON i.code_dotless = replace(ii.icd10_code,'.','');

        -- NEW: R5, the crosswalk the sanctioned prompt produced
        INSERT INTO indication_icd_v2
        SELECT DISTINCT
               'llm:' || md5(lower(btrim(l.indication))),
               coalesce(i.code_dotted, l.icd10_code),
               replace(l.icd10_code,'.',''),
               l.level,
               i.is_billable,
               'R5',
               'inference',
               coalesce(c.ccsr_cat1,''),
               coalesce(c.body_system,''),
               l.confidence * 0.9
        FROM llm_crosswalk l
        LEFT JOIN icd10cm i ON i.code_dotless = replace(l.icd10_code,'.','')
        LEFT JOIN ccsr c ON c.code_dotless = replace(l.icd10_code,'.','')
        WHERE l.icd10_code IS NOT NULL;

        CREATE INDEX ii2_ind ON indication_icd_v2 (indication_id);
        CREATE INDEX ii2_code ON indication_icd_v2 (icd10_code_dotless);
        ANALYZE indication_icd_v2;
    """)

    # ---- the confidence scale is ENFORCED, not merely applied -------------
    # A rule that is only applied is a rule that silently stops holding the
    # first time a route is added. 1.0 is the user-facing claim "we know
    # this", so a row that reaches it without a deterministic derivation is a
    # misrepresentation shipped to a person.
    cur.execute("""
        SELECT count(*) FILTER (WHERE provenance='inference' AND confidence > 0.9),
               count(*) FILTER (WHERE provenance<>'inference' AND confidence IS DISTINCT FROM 1.0),
               max(confidence) FILTER (WHERE provenance='inference'),
               min(confidence) FILTER (WHERE provenance<>'inference')
        FROM indication_icd_v2""")
    over, notone, maxinf, mindet = cur.fetchone()
    emit()
    emit("=== CONFIDENCE SCALE -- 1.0 deterministic, 0..0.9 inferred ===")
    emit("  highest confidence on an INFERRED fact      : %s" % maxinf)
    emit("  lowest confidence on a DETERMINISTIC fact   : %s" % mindet)
    emit("  inferred rows above the 0.9 ceiling         : %s" % f"{over:,}")
    emit("  deterministic rows not at exactly 1.0       : %s" % f"{notone:,}")
    if over or notone:
        raise ChatHealthyException(
            "runtime_error",
            "the confidence scale is violated: %d inferred rows exceed 0.9 and "
            "%d deterministic rows are not 1.0. Refusing to build a delivered "
            "set whose confidence column does not mean what we tell users it "
            "means." % (over, notone))
    emit("  scale holds")
    cur.execute("""SELECT route, provenance, count(*),
                          count(DISTINCT indication_id),
                          count(DISTINCT icd10_code)
                   FROM indication_icd_v2 GROUP BY 1,2 ORDER BY 3 DESC""")
    emit("  %-6s %-20s %10s %13s %11s"
         % ("route", "provenance", "rows", "indications", "codes"))
    for r, p, n, i_, c in cur.fetchall():
        emit("  %-6s %-20s %10s %13s %11s"
             % (r, p, f"{n:,}", f"{i_:,}", f"{c:,}"))

    # ---- hallucination, against the catalog the model was given -----------
    emit()
    emit("=== HALLUCINATION -- codes outside the 12,014-code catalog ===")
    cat = catalog_codes()
    emit("  catalog codes loaded: %s" % f"{len(cat):,}")
    cur.execute("""SELECT DISTINCT icd10_code FROM indication_icd_v2
                   WHERE route='R5' AND icd10_code IS NOT NULL""")
    r5 = {r[0] for r in cur.fetchall()}
    bad = sorted(c for c in r5 if c.replace(".", "").upper() not in cat)
    emit("  distinct R5 codes: %s   outside the catalog: %s"
         % (f"{len(r5):,}", f"{len(bad):,}"))
    emit("  hallucination rate: %.4f   (benchmark %.3f)"
         % (len(bad) / max(len(r5), 1), BENCH_HALLUCINATION))
    if bad:
        emit("  offenders: %s" % ", ".join(bad[:20]))

    # ---- does the loaded ICD-10-CM set recognise them? -------------------
    # Matched on BOTH forms. Comparing the model's dotless output against
    # code_dotted alone reported 969 fabrications where there is ONE -- the
    # difference was entirely dot format, not invented codes.
    cur.execute("""SELECT count(DISTINCT l.icd10_code_dotless)
                   FROM indication_icd_v2 l
                   WHERE l.route='R5'
                     AND NOT EXISTS (SELECT 1 FROM icd10cm i
                                     WHERE i.code_dotless=l.icd10_code_dotless)""")
    emit("  R5 codes in NEITHER form of the loaded 98,186-code set: %s"
         % f"{cur.fetchone()[0]:,}")

    # ================================================================== T5
    emit()
    emit("=== T5 the delivered set, on the billing-anchored T2 ===")
    cur.execute("""
        DROP TABLE IF EXISTS t5_delivered_v2;
        CREATE TABLE t5_delivered_v2 AS
        SELECT pm.fill_state,
               pm.billing_state,
               pm.state_differs,
               pm.npi,
               pm.entity_type,
               pm.primary_nucc,
               pm.primary_nucc_label,
               pm.molecule_id,
               mi.indication_id,
               mi.indication_text,
               mi.source      AS ind_source,
               mi.authority,
               mi.rung,
               ii.icd10_code,
               ii.icd10_code_dotless,
               ii.level       AS icd_level,
               ii.is_billable,
               ii.route,
               ii.ccsr_cat,
               ii.body_system,
               -- NAMED non-additive: the volume belongs to (npi, molecule_id)
               -- and is repeated across this row's fan-out. Summing this
               -- column is wrong. Rollups below distinct-count back to the
               -- additive grain instead.
               pm.tot_clms    AS molecule_tot_clms_nonadditive,
               pm.tot_drug_cst AS molecule_tot_cst_nonadditive,
               CASE WHEN 'inference' IN (mi.provenance, ii.provenance)
                      THEN 'inference'
                    WHEN 'adopted_vocabulary' IN (mi.provenance, ii.provenance)
                      THEN 'adopted_vocabulary'
                    ELSE 'joined_coded_source' END AS provenance
        FROM provider_molecule_v2 pm
        JOIN molecule_indication_v2 mi ON mi.molecule_id = pm.molecule_id
        JOIN indication_icd_v2 ii ON ii.indication_id = mi.indication_id;

        CREATE INDEX t52_fill ON t5_delivered_v2 (fill_state);
        CREATE INDEX t52_icd ON t5_delivered_v2 (icd10_code);
        CREATE INDEX t52_npi ON t5_delivered_v2 (npi);
        CREATE INDEX t52_mol ON t5_delivered_v2 (npi, molecule_id);
        CREATE INDEX t52_prov ON t5_delivered_v2 (provenance);
        ANALYZE t5_delivered_v2;
    """)
    cur.execute("""SELECT count(*), count(DISTINCT npi),
                          count(DISTINCT molecule_id),
                          count(DISTINCT indication_id),
                          count(DISTINCT icd10_code)
                   FROM t5_delivered_v2""")
    n, p, m, i_, c = cur.fetchone()
    emit("  rows=%s providers=%s molecules=%s indications=%s codes=%s"
         % (f"{n:,}", f"{p:,}", f"{m:,}", f"{i_:,}", f"{c:,}"))

    emit()
    emit("  by FILL state (where the prescription was filled):")
    cur.execute("""SELECT fill_state, count(*), count(DISTINCT npi),
                          count(DISTINCT icd10_code)
                   FROM t5_delivered_v2 GROUP BY 1 ORDER BY 1""")
    for s, rn, pn, cn in cur.fetchall():
        emit("    %-4s rows=%-13s providers=%-8s codes=%s"
             % (s, f"{rn:,}", f"{pn:,}", f"{cn:,}"))
    emit("  providers whose BILLING state differs from the fill state: ")
    cur.execute("""SELECT count(DISTINCT npi) FROM t5_delivered_v2
                   WHERE state_differs""")
    emit("    %s -- the two facts are carried separately and never "
         "substituted" % f"{cur.fetchone()[0]:,}")

    # ---- THE FIX, demonstrated -------------------------------------------
    emit()
    emit("=== THE FAN-OUT OVER-COUNT, BEFORE AND AFTER ===")
    cur.execute("SELECT sum(molecule_tot_clms_nonadditive) FROM t5_delivered_v2")
    naive = int(cur.fetchone()[0] or 0)
    cur.execute("""SELECT sum(tot_clms) FROM provider_molecule_v2""")
    truth = int(cur.fetchone()[0] or 0)
    emit("  naive SUM over every T5 row        : %s" % f"{naive:,}")
    emit("  the actual claim total at its grain: %s" % f"{truth:,}")
    emit("  inflation factor                   : %.1fx"
         % (naive / max(truth, 1)))
    emit("  -> this is exactly why the column is named _nonadditive, and why "
         "every figure below distinct-counts back to (npi, molecule_id).")

    emit()
    emit("  CORRECT per-code volume: the same two codes that used to tie")
    cur.execute("""
        WITH additive AS (
          SELECT DISTINCT icd10_code, npi, molecule_id,
                 molecule_tot_clms_nonadditive AS clms
          FROM t5_delivered_v2)
        SELECT icd10_code, count(DISTINCT npi) AS providers,
               sum(clms) AS claims
        FROM additive WHERE icd10_code IN ('I50','I50.81','I50.9','E11','E11.9')
        GROUP BY 1 ORDER BY 1""")
    emit("    %-9s %11s %14s" % ("code", "providers", "claims"))
    for code, pv, cl in cur.fetchall():
        emit("    %-9s %11s %14s" % (code, f"{pv:,}", f"{int(cl):,}"))

    emit()
    emit("  top 12 codes by CORRECTED claim volume:")
    cur.execute("""
        WITH additive AS (
          SELECT DISTINCT icd10_code, npi, molecule_id,
                 molecule_tot_clms_nonadditive AS clms
          FROM t5_delivered_v2)
        SELECT a.icd10_code, count(DISTINCT a.npi), sum(a.clms),
               max(i.short_desc)
        FROM additive a
        LEFT JOIN icd10cm i ON i.code_dotless=replace(a.icd10_code,'.','')
        GROUP BY 1 ORDER BY 3 DESC LIMIT 12""")
    emit("    %-9s %10s %14s  %s" % ("code", "providers", "claims", "description"))
    for code, pv, cl, desc in cur.fetchall():
        emit("    %-9s %10s %14s  %s"
             % (code, f"{pv:,}", f"{int(cl):,}", str(desc)[:42]))

    # ---- coverage, the business-weighted answer ---------------------------
    emit()
    emit("=== CHAIN COVERAGE, claim-weighted ===")
    cur.execute("""
        WITH reach AS (SELECT DISTINCT npi, molecule_id FROM t5_delivered_v2)
        SELECT sum(pm.tot_clms) FILTER (WHERE r.npi IS NOT NULL),
               sum(pm.tot_clms)
        FROM provider_molecule_v2 pm
        LEFT JOIN reach r ON r.npi=pm.npi AND r.molecule_id=pm.molecule_id""")
    reached, total = cur.fetchone()
    reached, total = int(reached or 0), int(total or 0)
    emit("  claims reaching at least one ICD code: %s of %s (%.2f%%)"
         % (f"{reached:,}", f"{total:,}", 100.0 * reached / max(total, 1)))

    cur.execute("""
        WITH reach AS (SELECT DISTINCT molecule_id FROM t5_delivered_v2)
        SELECT count(DISTINCT pm.molecule_id) FILTER (WHERE r.molecule_id IS NOT NULL),
               count(DISTINCT pm.molecule_id)
        FROM provider_molecule_v2 pm
        LEFT JOIN reach r ON r.molecule_id=pm.molecule_id""")
    rm, tm = cur.fetchone()
    emit("  molecules reaching at least one ICD code: %s of %s (%.2f%%)"
         % (f"{rm:,}", f"{tm:,}", 100.0 * rm / max(tm, 1)))

    emit()
    emit("  R5's own contribution -- what the inference added that the "
         "deterministic routes did not reach:")
    cur.execute("""
        WITH det AS (SELECT DISTINCT npi, molecule_id FROM t5_delivered_v2
                     WHERE route <> 'R5'),
             any_ AS (SELECT DISTINCT npi, molecule_id FROM t5_delivered_v2)
        SELECT sum(pm.tot_clms) FILTER (WHERE d.npi IS NOT NULL) AS det_clms,
               sum(pm.tot_clms) FILTER (WHERE a.npi IS NOT NULL) AS all_clms,
               sum(pm.tot_clms) AS tot
        FROM provider_molecule_v2 pm
        LEFT JOIN det d ON d.npi=pm.npi AND d.molecule_id=pm.molecule_id
        LEFT JOIN any_ a ON a.npi=pm.npi AND a.molecule_id=pm.molecule_id""")
    dc, ac, tc = [int(x or 0) for x in cur.fetchone()]
    emit("    deterministic routes only : %s of %s (%.2f%%)"
         % (f"{dc:,}", f"{tc:,}", 100.0 * dc / max(tc, 1)))
    emit("    with R5                   : %s of %s (%.2f%%)"
         % (f"{ac:,}", f"{tc:,}", 100.0 * ac / max(tc, 1)))
    emit("    R5 added                  : %.2f claim points"
         % (100.0 * (ac - dc) / max(tc, 1)))

    # ---- provenance mix at the DELIVERED grain ---------------------------
    emit()
    emit("=== PROVENANCE at the delivered grain, weakest-on-path ===")
    cur.execute("""
        WITH best AS (
          SELECT npi, molecule_id, icd10_code,
                 min(CASE provenance WHEN 'joined_coded_source' THEN 1
                                     WHEN 'adopted_vocabulary' THEN 2
                                     ELSE 3 END) AS rank_
          FROM t5_delivered_v2 GROUP BY 1,2,3),
         lab AS (SELECT npi, molecule_id, icd10_code,
                   CASE rank_ WHEN 1 THEN 'joined_coded_source'
                              WHEN 2 THEN 'adopted_vocabulary'
                              ELSE 'inference' END AS prov
                 FROM best)
        SELECT prov, count(*),
               round((100.0*count(*)/sum(count(*)) OVER ())::numeric,2)
        FROM lab GROUP BY 1 ORDER BY 2 DESC""")
    emit("    %-22s %13s %9s" % ("provenance", "delivered facts", "share"))
    for prov, n_, pct in cur.fetchall():
        emit("    %-22s %13s %8.2f%%" % (prov, f"{n_:,}", pct))
    emit("  read this as: the share of the delivered product that rests on a "
         "coded join versus on the model. A chain that is mostly inference is "
         "a different product from one that is mostly joined.")

    # ---- the candidate grains, per the design: report, do not choose -----
    emit()
    emit("=== CANDIDATE GRAINS -- reported, not chosen (design 4.8) ===")
    for label, sql in (
        ("provider x molecule",
         "SELECT count(*) FROM (SELECT DISTINCT npi,molecule_id FROM t5_delivered_v2) x"),
        ("provider x indication",
         "SELECT count(*) FROM (SELECT DISTINCT npi,indication_id FROM t5_delivered_v2) x"),
        ("provider x ICD code",
         "SELECT count(*) FROM (SELECT DISTINCT npi,icd10_code FROM t5_delivered_v2) x"),
        ("provider x ICD level 3 only",
         "SELECT count(*) FROM (SELECT DISTINCT npi,icd10_code FROM t5_delivered_v2 WHERE icd_level=3) x"),
        ("provider x CCSR category",
         "SELECT count(*) FROM (SELECT DISTINCT npi,ccsr_cat FROM t5_delivered_v2 WHERE ccsr_cat<>'') x"),
    ):
        cur.execute(sql)
        emit("    %-30s %14s rows" % (label, f"{cur.fetchone()[0]:,}"))

    con.close()
    emit()
    emit("written to " + OUT)


main()
