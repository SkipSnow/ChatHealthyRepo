"""Execution step 8 -- T4 with all deterministic routes, then T5, then holdout.

T4 routes, most authoritative first. A route claims an indication only if no
higher route already did.

  R1  coverage_icd10 via article_id        joined_coded_source
  R2  MeSH -> ICD-10-CM via UMLS crosswalk joined_coded_source
  R3  indication text -> icd10cm.long_desc adopted_vocabulary
  R4  CCSR category attached for rollup    adopted_vocabulary   (not a route)
  R5  model                                inference            (measured as the gap)

Level is taken from icd10cm.is_billable: a category header is level 3, a
billable leaf is level 4. Not derived from code length.

THE HOLDOUT. Every rule above was written while looking at both states. To test
it honestly the delivered set is rebuilt twice: once from molecules seen ONLY in
Alabama and once from molecules seen ONLY in Delaware, and each is scored
against the other state's claims. A rule that only works where it was written
shows up as a gap between the two.
"""
import sys
import os
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\exec_08_final_result.txt"


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
    emit("EXECUTION STEP 8 -- T4 all routes, T5, holdout")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()
    con = connect(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='3600s'")

    # =================================================== T4, routes in order
    emit("=== T4 indication_icd, routes in precedence order ===")
    cur.execute("DROP TABLE IF EXISTS indication_icd")
    cur.execute("""CREATE TABLE indication_icd (
                     indication_id text, icd10_code text, level int,
                     is_billable boolean, route text, provenance text,
                     ccsr_cat text, body_system text)""")

    # R1: the CMS Coverage Articles. An indication reaches an article only
    # through its own text, so R1 is tried by description equality against the
    # article's ICD description -- a coded source reached by its own wording.
    cur.execute("""
        INSERT INTO indication_icd
          (indication_id, icd10_code, level, is_billable, route, provenance)
        SELECT DISTINCT mi.indication_id, c.code_dotted,
               CASE WHEN i.is_billable THEN 4 ELSE 3 END, i.is_billable,
               'R1_coverage', 'joined_coded_source'
        FROM (SELECT DISTINCT indication_id, lower(indication_text) t
              FROM molecule_indication WHERE indication_id <> '') mi
        JOIN coverage_icd10 c ON lower(c.description) = mi.t
        JOIN icd10cm i ON i.code_dotted = c.code_dotted""")
    r1 = cur.rowcount
    emit("  R1 coverage            rows=%s" % f"{r1:,}")

    # R2: UMLS crosswalk, for any MeSH indication not already claimed
    cur.execute("""
        INSERT INTO indication_icd
          (indication_id, icd10_code, level, is_billable, route, provenance)
        SELECT DISTINCT mi.indication_id, m.icd10_code,
               CASE WHEN i.is_billable THEN 4 ELSE 3 END, i.is_billable,
               'R2_umls_crosswalk', 'joined_coded_source'
        FROM (SELECT DISTINCT indication_id FROM molecule_indication
              WHERE indication_id LIKE 'mesh:%') mi
        JOIN mesh_icd_umls m
          ON m.mesh_id = replace(mi.indication_id,'mesh:','')
        JOIN icd10cm i ON i.code_dotted = m.icd10_code
        WHERE NOT EXISTS (SELECT 1 FROM indication_icd x
                          WHERE x.indication_id = mi.indication_id)""")
    r2 = cur.rowcount
    emit("  R2 UMLS crosswalk      rows=%s" % f"{r2:,}")

    # R3: vocabulary match, only for what R1 and R2 left
    cur.execute("""
        INSERT INTO indication_icd
          (indication_id, icd10_code, level, is_billable, route, provenance)
        SELECT DISTINCT mi.indication_id, i.code_dotted,
               CASE WHEN i.is_billable THEN 4 ELSE 3 END, i.is_billable,
               'R3_vocabulary', 'adopted_vocabulary'
        FROM (SELECT DISTINCT indication_id, lower(indication_text) t
              FROM molecule_indication WHERE indication_id <> '') mi
        JOIN icd10cm i ON lower(i.long_desc) = mi.t
        WHERE NOT EXISTS (SELECT 1 FROM indication_icd x
                          WHERE x.indication_id = mi.indication_id)""")
    r3 = cur.rowcount
    emit("  R3 vocabulary          rows=%s" % f"{r3:,}")

    for ddl in ("CREATE INDEX t4_ind ON indication_icd (indication_id)",
                "CREATE INDEX t4_code ON indication_icd (icd10_code)",
                "CREATE INDEX t4_route ON indication_icd (route)"):
        cur.execute(ddl)
    # R4: the CCSR clinical rollup, attached rather than a route
    cur.execute("""UPDATE indication_icd t SET ccsr_cat = x.ccsr_cat1,
                          body_system = x.body_system
                   FROM ccsr x WHERE x.code_dotted = t.icd10_code""")
    cur.execute("ANALYZE indication_icd")

    emit()
    cur.execute("""SELECT route, provenance, count(*),
                      count(DISTINCT indication_id), count(DISTINCT icd10_code),
                      count(*) FILTER (WHERE level=3),
                      count(*) FILTER (WHERE level=4)
                   FROM indication_icd GROUP BY 1,2 ORDER BY 1""")
    emit("  %-20s %-21s %8s %12s %8s %8s %8s"
         % ("route", "provenance", "rows", "indications", "codes", "L3", "L4"))
    for r, p, n, i_, c, l3, l4 in cur.fetchall():
        emit("  %-20s %-21s %8s %12s %8s %8s %8s"
             % (r, p, f"{n:,}", f"{i_:,}", f"{c:,}", f"{l3:,}", f"{l4:,}"))

    cur.execute("SELECT count(DISTINCT indication_id) FROM molecule_indication WHERE indication_id<>''")
    tot = cur.fetchone()[0]
    cur.execute("SELECT count(DISTINCT indication_id) FROM indication_icd")
    got = cur.fetchone()[0]
    emit()
    emit("  indications reaching an ICD code: %s of %s (%.1f%%)"
         % (f"{got:,}", f"{tot:,}", 100.0 * got / max(tot, 1)))
    emit("  R5 (model) would have to cover the remaining %s (%.1f%%)"
         % (f"{tot-got:,}", 100.0 * (tot - got) / max(tot, 1)))
    cur.execute("""SELECT count(*) FILTER (WHERE ccsr_cat IS NOT NULL AND ccsr_cat<>''),
                          count(*) FROM indication_icd""")
    a, b = cur.fetchone()
    emit("  R4 CCSR category attached to %s of %s rows (%.1f%%)"
         % (f"{a:,}", f"{b:,}", 100.0 * a / max(b, 1)))

    # ============================================================== T5
    emit()
    emit("=== T5 the delivered set ===")
    cur.execute("""
        DROP TABLE IF EXISTS t5_delivered;
        CREATE TABLE t5_delivered AS
        SELECT pm.state, pm.prscrbr_npi, pm.molecule_id,
               mi.indication_id, mi.source AS ind_source, mi.authority,
               mi.rung, ii.icd10_code, ii.level AS icd_level, ii.route,
               ii.ccsr_cat, ii.body_system, pm.tot_clms, pm.tot_drug_cst,
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
        CREATE INDEX t5_prov ON t5_delivered (provenance);
    """)
    cur.execute("""SELECT state, count(*), count(DISTINCT prscrbr_npi),
                          count(DISTINCT molecule_id), count(DISTINCT indication_id),
                          count(DISTINCT icd10_code)
                   FROM t5_delivered GROUP BY 1 ORDER BY 1""")
    emit("  %-5s %12s %11s %11s %13s %11s"
         % ("state", "rows", "providers", "molecules", "indications", "ICD codes"))
    for s, n, p, m, i_, c in cur.fetchall():
        emit("  %-5s %12s %11s %11s %13s %11s"
             % (s, f"{n:,}", f"{p:,}", f"{m:,}", f"{i_:,}", f"{c:,}"))

    emit()
    emit("  PROVENANCE MIX -- the share resting on a coded join:")
    cur.execute("""SELECT provenance, count(*),
                      round((100.0*count(*)/sum(count(*)) OVER ())::numeric,2)
                   FROM t5_delivered GROUP BY 1 ORDER BY 2 DESC""")
    for p, n, pct in cur.fetchall():
        emit("    %-22s %13s %7.2f%%" % (p, f"{n:,}", pct))
    emit()
    emit("  AUTHORITY MIX -- approved label vs investigational trial:")
    cur.execute("""SELECT authority, count(*),
                      round((100.0*count(*)/sum(count(*)) OVER ())::numeric,2)
                   FROM t5_delivered GROUP BY 1 ORDER BY 2 DESC""")
    for a_, n, pct in cur.fetchall():
        emit("    %-22s %13s %7.2f%%" % (a_, f"{n:,}", pct))
    emit()
    emit("  LEVEL MIX:")
    cur.execute("""SELECT icd_level, count(*), count(DISTINCT icd10_code),
                      round((100.0*count(*)/sum(count(*)) OVER ())::numeric,2)
                   FROM t5_delivered GROUP BY 1 ORDER BY 1""")
    for lv, n, c, pct in cur.fetchall():
        emit("    level %-3s rows=%-13s codes=%-7s %6.2f%%"
             % (lv, f"{n:,}", f"{c:,}", pct))

    emit()
    emit("  CANDIDATE GRAINS -- row count at each, so the choice is informed:")
    for label, sql in (
        ("provider x molecule",
         "SELECT count(*) FROM (SELECT DISTINCT state,prscrbr_npi,molecule_id FROM t5_delivered) x"),
        ("provider x indication",
         "SELECT count(*) FROM (SELECT DISTINCT state,prscrbr_npi,indication_id FROM t5_delivered) x"),
        ("provider x ICD code",
         "SELECT count(*) FROM (SELECT DISTINCT state,prscrbr_npi,icd10_code FROM t5_delivered) x"),
        ("provider x ICD code, level 3 only",
         "SELECT count(*) FROM (SELECT DISTINCT state,prscrbr_npi,icd10_code FROM t5_delivered WHERE icd_level=3) x"),
        ("provider x CCSR category",
         "SELECT count(*) FROM (SELECT DISTINCT state,prscrbr_npi,ccsr_cat FROM t5_delivered WHERE ccsr_cat<>'') x"),
    ):
        cur.execute(sql)
        emit("    %-36s %13s rows" % (label, f"{cur.fetchone()[0]:,}"))

    emit()
    emit("  PROVIDER REACH:")
    cur.execute("""SELECT c.state, count(DISTINCT c.prscrbr_npi),
                          count(DISTINCT d.prscrbr_npi)
                   FROM provider_coverage c
                   LEFT JOIN t5_delivered d
                     ON d.state=c.state AND d.prscrbr_npi=c.prscrbr_npi
                   GROUP BY 1 ORDER BY 1""")
    for s, allp, reach in cur.fetchall():
        emit("    %-4s %s of %s providers reachable by an ICD query (%.1f%%)"
             % (s, f"{reach:,}", f"{allp:,}", 100.0 * reach / allp))

    # ========================================================== HOLDOUT
    emit()
    emit("=== HOLDOUT -- rules written on one state, scored on the other ===")
    cur.execute("""
        DROP TABLE IF EXISTS mol_origin;
        CREATE TABLE mol_origin AS
        SELECT t.molecule_id,
               bool_or(s.state='DE') AS in_de,
               bool_or(s.state='AL') AS in_al
        FROM partd_pair_state s
        JOIN cms_drug_resolution t USING (brnd_name, gnrc_name)
        WHERE t.molecule_id <> ''
        GROUP BY 1;
    """)
    cur.execute("""SELECT count(*) FILTER (WHERE in_de AND in_al),
                          count(*) FILTER (WHERE in_de AND NOT in_al),
                          count(*) FILTER (WHERE in_al AND NOT in_de)
                   FROM mol_origin""")
    both, de_only, al_only = cur.fetchone()
    emit("  molecules in both states=%s  DE only=%s  AL only=%s"
         % (f"{both:,}", f"{de_only:,}", f"{al_only:,}"))
    emit("  A molecule seen in only one state was never visible while the rules")
    emit("  were written against the other. Those are the honest unseen cases.")
    emit()
    cur.execute("""
        SELECT o.in_de AND o.in_al AS shared,
               count(DISTINCT o.molecule_id) AS molecules,
               count(DISTINCT CASE WHEN mi.indication_id <> ''
                                   THEN o.molecule_id END) AS with_indication,
               count(DISTINCT CASE WHEN ii.icd10_code IS NOT NULL
                                   THEN o.molecule_id END) AS with_icd
        FROM mol_origin o
        LEFT JOIN molecule_indication mi ON mi.molecule_id=o.molecule_id
        LEFT JOIN indication_icd ii ON ii.indication_id=mi.indication_id
        GROUP BY 1 ORDER BY 1 DESC""")
    emit("  %-28s %11s %16s %12s" % ("population", "molecules", "with indication",
                                     "with ICD"))
    for shared, m, wi, wc in cur.fetchall():
        name = "seen in BOTH states" if shared else "seen in ONE state only"
        emit("  %-28s %11s %15s%% %11s%%"
             % (name, f"{m:,}", round(100.0*wi/max(m,1),1), round(100.0*wc/max(m,1),1)))
    emit()
    emit("  A gap between these two rows is the rules failing on unseen drugs.")

    emit()
    emit("=== TOP ICD CODES BY CLAIMS, EACH STATE, LEVEL 3 ===")
    for st in ("DE", "AL"):
        cur.execute("""SELECT d.icd10_code, max(c.long_desc),
                              count(DISTINCT d.prscrbr_npi), sum(d.tot_clms),
                              max(d.route)
                       FROM t5_delivered d
                       LEFT JOIN icd10cm c ON c.code_dotted=d.icd10_code
                       WHERE d.state=%s AND d.icd_level=3
                       GROUP BY 1 ORDER BY 4 DESC LIMIT 8""", (st,))
        emit("  %s:" % st)
        for code, desc, np_, cl, rt in cur.fetchall():
            emit("    %-9s %-40s prov=%-7s claims=%-12s %s"
                 % (code, str(desc)[:40], f"{np_:,}", f"{cl:,.0f}", rt))
    con.close()
    emit()
    emit("written to " + OUT)


main()
