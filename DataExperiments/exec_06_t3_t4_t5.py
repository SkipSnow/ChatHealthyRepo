"""Execution step 6 -- T3, T4 and T5 end to end, Delaware and Alabama.

T3 molecule_indication   per (molecule_id, indication_id, source)
T4 indication_icd        per (indication_id, icd10_code) with level 3 and 4
T5 the delivered set     provider -> molecule -> indication -> ICD

Normalisation ladder, most authoritative first. Each rung stamps its own
provenance and every rung is separately reportable, so the share of the
delivered set resting on inference is a measured fact.

  N1  the trial's OWN MeSH descriptor becomes the indication_id     joined_coded_source
  N2  a label indication naming a MeSH term adopts that descriptor  adopted_vocabulary
  N3  a label indication naming an ICD-10-CM description adopts it  adopted_vocabulary
  N4  extraction and canonicalisation by model                      inference
  N5  nothing matched: verbatim text, null id, COUNTED              unresolved

T4 routes:
  R1  coverage_icd10 article data                                   joined_coded_source
  R3  indication text -> icd10cm.long_desc                          adopted_vocabulary
  R4  icd10cm -> CCSR category, for the clinical rollup              adopted_vocabulary
  R5  model                                                         inference

N4 and R5 are NOT executed in this pass. The deterministic rungs run first and
the residual they leave is enumerated, because the purpose is to find how much
inference is actually required rather than to reach for it.

FDA and trial facts stay separate: T3 carries source and authority per row and
nothing merges an investigational condition into an approved one.
"""
import sys
import csv
import io
import os
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\exec_06_chain_result_v2.txt"

# A MeSH term shorter than this matches too much prose to be trusted.
MIN_TERM = 7
# Trial statuses whose condition may be admitted as an investigational fact.
ADMIT_STATUS = ("COMPLETED", "RECRUITING", "ACTIVE_NOT_RECRUITING",
                "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING", "AVAILABLE")


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("EXECUTION STEP 6 -- T3, T4, T5 for Delaware and Alabama")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()
    con = connect(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='3600s'")

    # ================================================================== T3
    emit("=== T3 molecule_indication ===")

    # molecule -> its FDA labels, in THREE RUNGS.
    #
    # This block previously joined f.ident_set = t.ingredient_set -- the
    # UNFOLDED identity key -- while its own comment said "via the FOLDED
    # match set". BR-016 is that MATCHING folds salt to base and IDENTITY
    # does not; this is a match, so it must use the fold. With the unfolded
    # key, CMS's atorvastatin base (rx:83366) never met its label's salt
    # (rx:1297766) and the single largest drug in the file -- 1.3M claims --
    # reached no diagnosis code at all. 2,847,156 claims were lost this way.
    #
    # The fix is here, where the defect is, and not patched afterwards:
    # molecule_indication is built FROM mol_label further down, so a repair
    # applied later would leave the indications standing on the defect.
    cur.execute("""
        DROP TABLE IF EXISTS mol_label;
        CREATE TABLE mol_label (molecule_id text, set_id text, rung text);

        -- rung 1: the FOLDED match key on both sides
        INSERT INTO mol_label
        SELECT DISTINCT t.molecule_id, f.set_id, 'folded_ingredient'
        FROM cms_drug_resolution t
        JOIN fda_mol f ON f.match_set = t.match_set
        WHERE t.molecule_id IS NOT NULL AND t.molecule_id <> '';

        -- rung 2: brand exact. Brand-exact is already a sanctioned rung of
        -- the resolution ladder; it was simply never applied to the LABEL
        -- match, which lost Trelegy, Lantus, Biktarvy, Paxlovid and Anoro --
        -- products whose labels were in the corpus the whole time.
        INSERT INTO mol_label
        SELECT DISTINCT r.molecule_id, b.set_id, 'brand_exact'
        FROM cms_drug_resolution r
        JOIN fda_brand_norm b ON lower(btrim(b.nm)) = lower(btrim(r.brnd_name))
        WHERE r.molecule_id IS NOT NULL AND r.molecule_id <> ''
          AND NOT EXISTS (SELECT 1 FROM mol_label ml
                          WHERE ml.molecule_id = r.molecule_id);
    """)
    # rung 3: CMS appends the delivery device to the brand -- 'Lantus
    # Solostar' against the label's 'Lantus'. Longest FDA brand wins, with a
    # five-character floor, so a short brand cannot capture another product.
    cur.execute("""
        DROP TABLE IF EXISTS tmp_dev_rung;
        CREATE TABLE tmp_dev_rung AS
        WITH unmatched AS (
          SELECT DISTINCT r.molecule_id, lower(btrim(r.brnd_name)) AS bn
          FROM cms_drug_resolution r
          WHERE r.molecule_id IS NOT NULL AND r.molecule_id <> ''
            AND NOT EXISTS (SELECT 1 FROM mol_label ml
                            WHERE ml.molecule_id = r.molecule_id))
        SELECT u.molecule_id, b.set_id, length(btrim(b.nm)) AS fda_len
        FROM unmatched u
        JOIN fda_brand_norm b
          ON u.bn LIKE lower(btrim(b.nm)) || '%'
         AND length(btrim(b.nm)) >= 5;

        INSERT INTO mol_label
        SELECT DISTINCT d.molecule_id, d.set_id, 'device_suffix'
        FROM tmp_dev_rung d
        JOIN (SELECT molecule_id, max(fda_len) best
              FROM tmp_dev_rung GROUP BY 1) m
          ON m.molecule_id = d.molecule_id AND d.fda_len = m.best;
        DROP TABLE tmp_dev_rung;

        CREATE INDEX ml_mol ON mol_label (molecule_id);
        CREATE INDEX ml_sid ON mol_label (set_id);
        ANALYZE mol_label;
    """)
    cur.execute("""SELECT rung, count(*), count(DISTINCT molecule_id)
                   FROM mol_label GROUP BY 1 ORDER BY 2 DESC""")
    for _r, _n, _m in cur.fetchall():
        emit("  rung %-20s %10s links  %6s molecules"
             % (_r, f"{_n:,}", f"{_m:,}"))
    cur.execute("SELECT count(DISTINCT molecule_id), count(*) FROM mol_label")
    a, b = cur.fetchone()
    emit("  molecules with at least one FDA label: %s (%s molecule-label links)"
         % (f"{a:,}", f"{b:,}"))

    # molecule -> its trials, through the artifact-free bridge in reverse
    cur.execute("""
        DROP TABLE IF EXISTS mol_trial;
        CREATE TABLE mol_trial AS
        SELECT DISTINCT t.molecule_id, i.nct_id
        FROM cms_drug_resolution t
        JOIN ct_bridge_t1 b
          ON b.n_ing = 1 AND ('rx:' || b.ing_rxcui) = t.molecule_id
        JOIN ct_intervention i
          ON upper(i.name) = b.src OR upper(i.other_name) = b.src
        WHERE t.molecule_id <> '';
        CREATE INDEX mt_mol ON mol_trial (molecule_id);
        CREATE INDEX mt_nct ON mol_trial (nct_id);
    """)
    cur.execute("SELECT count(DISTINCT molecule_id), count(*) FROM mol_trial")
    a, b = cur.fetchone()
    emit("  molecules with at least one trial    : %s (%s molecule-trial links)"
         % (f"{a:,}", f"{b:,}"))

    # ---- N1: the trial's own MeSH descriptor ------------------------------
    cur.execute("""
        DROP TABLE IF EXISTS t3_n1;
        CREATE TABLE t3_n1 AS
        SELECT DISTINCT mt.molecule_id,
               'mesh:' || m.mesh_id        AS indication_id,
               m.mesh_term                 AS indication_text,
               'trial'                     AS source,
               'investigational'           AS authority,
               'N1'                        AS rung,
               'joined_coded_source'       AS provenance
        FROM mol_trial mt
        JOIN ct_study s ON s.nct_id = mt.nct_id
                       AND s.overall_status = ANY(%s)
        JOIN ct_mesh m ON m.nct_id = mt.nct_id AND NOT m.is_ancestor
        WHERE m.mesh_id <> '';
    """, (list(ADMIT_STATUS),))
    cur.execute("SELECT count(*), count(DISTINCT molecule_id), count(DISTINCT indication_id) FROM t3_n1")
    a, b, c = cur.fetchone()
    emit("  N1 trial MeSH      rows=%s molecules=%s indications=%s"
         % (f"{a:,}", f"{b:,}", f"{c:,}"))

    # ---- the MeSH and ICD vocabularies, for N2 and N3 ---------------------
    cur.execute("""SELECT DISTINCT mesh_id, lower(mesh_term) FROM ct_mesh
                   WHERE mesh_id <> '' AND length(mesh_term) >= %s""", (MIN_TERM,))
    mesh = [(t, i) for i, t in cur.fetchall()]
    mesh.sort(key=lambda x: -len(x[0]))          # longest first
    emit("  MeSH vocabulary for N2: %s terms (>= %d chars)"
         % (f"{len(mesh):,}", MIN_TERM))

    cur.execute("""SELECT lower(long_desc), code_dotted FROM icd10cm
                   WHERE long_desc <> '' AND length(long_desc) >= %s""", (MIN_TERM,))
    icd_by_desc = {}
    for d, c in cur.fetchall():
        icd_by_desc.setdefault(d, c)
    emit("  ICD vocabulary for N3: %s descriptions" % f"{len(icd_by_desc):,}")

    # ---- the label prose per molecule -------------------------------------
    cur.execute("""
        SELECT ml.molecule_id,
               lower(string_agg(DISTINCT coalesce(l.indications_and_usage,''), ' '))
        FROM mol_label ml
        JOIN (SELECT DISTINCT set_id, indications_and_usage FROM fda_label) l
          ON l.set_id = ml.set_id
        GROUP BY 1""")
    prose = cur.fetchall()
    emit("  molecules with label prose: %s" % f"{len(prose):,}")

    n2_rows, n3_rows, n5_rows = [], [], []
    t0 = time.time()
    for i, (mol, text) in enumerate(prose, 1):
        text = text or ""
        hits = 0
        if text:
            for term, mid in mesh:
                if term in text:
                    n2_rows.append([mol, "mesh:" + mid, term, "fda_label",
                                    "approved", "N2", "adopted_vocabulary"])
                    hits += 1
                    if hits >= 25:
                        break
            if hits == 0:
                for desc, code in icd_by_desc.items():
                    if desc in text:
                        n3_rows.append([mol, "icd:" + code, desc, "fda_label",
                                        "approved", "N3", "adopted_vocabulary"])
                        hits += 1
                        if hits >= 10:
                            break
        if hits == 0:
            n5_rows.append([mol, "", (text[:180] if text else ""), "fda_label",
                            "approved", "N5", "unresolved"])
        if i % 250 == 0:
            emit("    scanned %s/%s molecules  %.0fs"
                 % (f"{i:,}", f"{len(prose):,}", time.time() - t0))

    cur.execute("""DROP TABLE IF EXISTS molecule_indication;
        CREATE TABLE molecule_indication (
          molecule_id text, indication_id text, indication_text text,
          source text, authority text, rung text, provenance text)""")
    buf = io.StringIO(); w = csv.writer(buf)
    for r in n2_rows + n3_rows + n5_rows:
        w.writerow(r)
    buf.seek(0)
    cur.copy_expert("COPY molecule_indication FROM STDIN WITH (FORMAT csv)", buf)
    cur.execute("""INSERT INTO molecule_indication
                   SELECT molecule_id, indication_id, indication_text, source,
                          authority, rung, provenance FROM t3_n1""")
    for ddl in ("CREATE INDEX t3_mol ON molecule_indication (molecule_id)",
                "CREATE INDEX t3_ind ON molecule_indication (indication_id)",
                "CREATE INDEX t3_rung ON molecule_indication (rung)"):
        cur.execute(ddl)
    cur.execute("ANALYZE molecule_indication")

    emit()
    emit("  T3 by rung:")
    cur.execute("""SELECT rung, provenance, source, authority, count(*),
                          count(DISTINCT molecule_id), count(DISTINCT indication_id)
                   FROM molecule_indication GROUP BY 1,2,3,4 ORDER BY 1""")
    emit("    %-5s %-21s %-11s %-17s %9s %10s %12s"
         % ("rung", "provenance", "source", "authority", "rows", "molecules",
            "indications"))
    for r, p, s, au, n, m, ind in cur.fetchall():
        emit("    %-5s %-21s %-11s %-17s %9s %10s %12s"
             % (r, p, s, au, f"{n:,}", f"{m:,}", f"{ind:,}"))

    # A COVERAGE PRINT. Its denominator comes from provider_molecule, which is
    # on the provider path, not this one. It threw and killed a whole run.
    # Reports never stop a build.
    try:
        cur.execute("""SELECT count(DISTINCT molecule_id) FROM molecule_indication
                       WHERE indication_id <> ''""")
        with_ind = cur.fetchone()[0]
        cur.execute("SELECT count(DISTINCT molecule_id) FROM provider_molecule")
        all_mol = cur.fetchone()[0]
        emit("  molecules with at least one NORMALISED indication: %s of %s (%.1f%%)"
             % (f"{with_ind:,}", f"{all_mol:,}", 100.0 * with_ind / all_mol))
    except Exception as exc:
        con.rollback() if not con.autocommit else None
        emit("  (coverage print skipped: %s -- build unaffected)"
             % type(exc).__name__)

    # ================================================================== T4
    emit()
    emit("=== T4 indication_icd ===")
    cur.execute("""
        DROP TABLE IF EXISTS indication_icd;
        CREATE TABLE indication_icd AS
        -- an indication that IS an ICD code needs no crosswalk
        SELECT DISTINCT i.indication_id, c.code_dotted AS icd10_code,
               CASE WHEN c.is_billable THEN 4 ELSE 3 END AS level, c.is_billable, 'R3_direct' AS route,
               'adopted_vocabulary' AS provenance
        FROM (SELECT DISTINCT indication_id FROM molecule_indication
              WHERE indication_id LIKE 'icd:%') i
        JOIN icd10cm c ON c.code_dotted = replace(i.indication_id,'icd:','')
        UNION
        -- a MeSH indication reaches ICD by description equality
        SELECT DISTINCT i.indication_id, c.code_dotted,
               CASE WHEN c.is_billable THEN 4 ELSE 3 END, c.is_billable,
               'R3_mesh_desc', 'adopted_vocabulary'
        FROM (SELECT DISTINCT indication_id, lower(indication_text) AS t
              FROM molecule_indication WHERE indication_id LIKE 'mesh:%') i
        JOIN icd10cm c ON lower(c.long_desc) = i.t;
        CREATE INDEX t4_ind ON indication_icd (indication_id);
        CREATE INDEX t4_code ON indication_icd (icd10_code);
    """)
    cur.execute("""SELECT route, count(*), count(DISTINCT indication_id),
                          count(DISTINCT icd10_code),
                          count(*) FILTER (WHERE level=3),
                          count(*) FILTER (WHERE level>3)
                   FROM indication_icd GROUP BY 1 ORDER BY 1""")
    emit("  %-16s %8s %12s %10s %9s %9s"
         % ("route", "rows", "indications", "codes", "level 3", "level 4+"))
    for r, n, ind, cd, l3, l4 in cur.fetchall():
        emit("  %-16s %8s %12s %10s %9s %9s"
             % (r, f"{n:,}", f"{ind:,}", f"{cd:,}", f"{l3:,}", f"{l4:,}"))
    cur.execute("SELECT count(DISTINCT indication_id) FROM molecule_indication WHERE indication_id<>''")
    tot_ind = cur.fetchone()[0]
    cur.execute("SELECT count(DISTINCT indication_id) FROM indication_icd")
    map_ind = cur.fetchone()[0]
    emit("  indications reaching ANY ICD code: %s of %s (%.1f%%)"
         % (f"{map_ind:,}", f"{tot_ind:,}", 100.0 * map_ind / max(tot_ind, 1)))
    emit("  -> the shortfall is what R2 (MeSH->UMLS->ICD, needs MRCONSO) and "
         "R5 (model) would have to cover")

    # add the CCSR rollup
    cur.execute("""ALTER TABLE indication_icd ADD COLUMN ccsr_cat text,
                                             ADD COLUMN body_system text""")
    cur.execute("""UPDATE indication_icd t SET ccsr_cat = x.ccsr_cat1,
                          body_system = x.body_system
                   FROM ccsr x WHERE x.code_dotted = t.icd10_code""")
    cur.execute("""SELECT count(*) FILTER (WHERE ccsr_cat IS NOT NULL AND ccsr_cat<>''),
                          count(*) FROM indication_icd""")
    a, b = cur.fetchone()
    emit("  R4 CCSR clinical category attached to %s of %s rows (%.1f%%)"
         % (f"{a:,}", f"{b:,}", 100.0 * a / max(b, 1)))

    # ================================================================== T5
    emit()
    emit("=== T5 the delivered set ===")
    cur.execute("""
        DROP TABLE IF EXISTS t5_delivered;
        CREATE TABLE t5_delivered AS
        SELECT pm.state, pm.prscrbr_npi, pm.molecule_id,
               mi.indication_id, mi.source AS ind_source,
               mi.authority, mi.rung,
               ii.icd10_code, ii.level AS icd_level, ii.ccsr_cat,
               ii.body_system,
               pm.tot_clms, pm.tot_drug_cst,
               -- the weakest provenance on the path survives to the row
               CASE WHEN 'inference' IN (mi.provenance, ii.provenance) THEN 'inference'
                    WHEN 'adopted_vocabulary' IN (mi.provenance, ii.provenance)
                      THEN 'adopted_vocabulary'
                    ELSE 'joined_coded_source' END AS provenance
        FROM provider_molecule pm
        JOIN molecule_indication mi ON mi.molecule_id = pm.molecule_id
                                   AND mi.indication_id <> ''
        JOIN indication_icd ii ON ii.indication_id = mi.indication_id;
        CREATE INDEX t5_state ON t5_delivered (state);
        CREATE INDEX t5_icd ON t5_delivered (icd10_code);
        CREATE INDEX t5_npi ON t5_delivered (state, prscrbr_npi);
    """)
    cur.execute("""SELECT state, count(*), count(DISTINCT prscrbr_npi),
                          count(DISTINCT molecule_id), count(DISTINCT indication_id),
                          count(DISTINCT icd10_code)
                   FROM t5_delivered GROUP BY 1 ORDER BY 1""")
    emit("  %-5s %12s %11s %11s %13s %10s"
         % ("state", "T5 rows", "providers", "molecules", "indications", "ICD codes"))
    for s, n, p, m, i_, c in cur.fetchall():
        emit("  %-5s %12s %11s %11s %13s %10s"
             % (s, f"{n:,}", f"{p:,}", f"{m:,}", f"{i_:,}", f"{c:,}"))

    emit()
    emit("  provenance mix of the delivered set:")
    cur.execute("""SELECT provenance, count(*),
                      round((100.0*count(*)/sum(count(*)) OVER ())::numeric,2)
                   FROM t5_delivered GROUP BY 1 ORDER BY 2 DESC""")
    for p, n, pct in cur.fetchall():
        emit("    %-22s %12s %7.2f%%" % (p, f"{n:,}", pct))
    emit()
    emit("  authority mix (approved vs investigational):")
    cur.execute("""SELECT authority, count(*),
                      round((100.0*count(*)/sum(count(*)) OVER ())::numeric,2)
                   FROM t5_delivered GROUP BY 1 ORDER BY 2 DESC""")
    for a_, n, pct in cur.fetchall():
        emit("    %-22s %12s %7.2f%%" % (a_, f"{n:,}", pct))

    emit()
    emit("  PROVIDER REACH -- what fraction of each state's providers are "
         "reachable by an ICD query:")
    cur.execute("""SELECT c.state, count(DISTINCT c.prscrbr_npi) AS all_prov,
                          count(DISTINCT d.prscrbr_npi) AS reachable
                   FROM provider_coverage c
                   LEFT JOIN t5_delivered d
                     ON d.state=c.state AND d.prscrbr_npi=c.prscrbr_npi
                   GROUP BY 1 ORDER BY 1""")
    for s, allp, reach in cur.fetchall():
        emit("    %-4s %s of %s providers (%.1f%%)"
             % (s, f"{reach:,}", f"{allp:,}", 100.0 * reach / allp))

    emit()
    emit("  worked example -- top ICD codes by claim volume, each state:")
    for st in ("DE", "AL"):
        cur.execute("""SELECT d.icd10_code, max(c.long_desc), d.icd_level,
                              count(DISTINCT d.prscrbr_npi), sum(d.tot_clms)
                       FROM t5_delivered d
                       LEFT JOIN icd10cm c ON c.code_dotted = d.icd10_code
                       WHERE d.state=%s GROUP BY 1,3 ORDER BY 5 DESC LIMIT 6""",
                    (st,))
        emit("    %s:" % st)
        for code, desc, lvl, np_, cl in cur.fetchall():
            emit("      %-9s L%-2s %-44s providers=%-6s claims=%s"
                 % (code, lvl, str(desc)[:44], f"{np_:,}", f"{cl:,.0f}"))
    con.close()
    emit()
    emit("written to " + OUT)


main()
