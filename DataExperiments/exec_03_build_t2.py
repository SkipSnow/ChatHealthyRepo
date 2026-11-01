"""Execution step 3 -- build T2, provider_molecule, for Delaware and Alabama.

Grain: one row per (state, prscrbr_npi, molecule_id).

TWO SEPARATIONS ARE STRUCTURAL HERE, both per operator ruling:

  STATE      state is part of the grain and a leading index column. There is no
             code path that sums Delaware and Alabama into one figure.

  FORMULATION molecule_id is the PRECISE, unfolded RxNorm concept set carried
             through from T1. Metoprolol succinate and metoprolol tartrate are
             different molecule_ids and are never summed together.

NO INFERENCE. T2 is joins and sums only. If any value here ever needs
inference, something upstream is mis-specified.

unresolved_clms is carried per provider so a provider at 60% coverage never
looks like one at 99%.

KNOWN GAP: the design specifies nucc_codes joined from the Provider collection
by NPI. That collection is on the paused pipeline cluster. NPPES is archived
locally and is a loadable step, not done here. prscrbr_type is carried instead
and is labelled claim-specialty text -- it is NOT the NUCC specialty and must
not be used as one.
"""
import sys
import os
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\exec_03_t2_result.txt"


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
    emit("EXECUTION STEP 3 -- T2 provider_molecule, Delaware and Alabama")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()
    con = connect(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='1800s'")

    cur.execute("DROP TABLE IF EXISTS provider_molecule")
    cur.execute("""
        CREATE TABLE provider_molecule AS
        SELECT
          p.prscrbr_state_abrvtn                          AS state,
          p.prscrbr_npi                                   AS prscrbr_npi,
          max(p.prscrbr_last_org_name)                    AS prscrbr_last_org_name,
          max(p.prscrbr_first_name)                       AS prscrbr_first_name,
          max(p.prscrbr_city)                             AS prscrbr_city,
          -- claim-specialty TEXT, not NUCC. Never to be used as the specialty.
          max(p.prscrbr_type)                             AS claim_specialty_text,
          t.molecule_id                                   AS molecule_id,
          count(DISTINCT (p.brnd_name, p.gnrc_name))      AS n_source_pairs,
          count(*)                                        AS src_rows,
          sum(NULLIF(p.tot_clms,'')::double precision)    AS tot_clms,
          sum(NULLIF(p.tot_day_suply,'')::double precision)  AS tot_day_suply,
          sum(NULLIF(p.tot_drug_cst,'')::double precision)   AS tot_drug_cst,
          -- a suppressed beneficiary count is NULL, never 0
          sum(NULLIF(p.tot_benes,'')::double precision)   AS tot_benes,
          count(*) FILTER (WHERE NULLIF(p.tot_benes,'') IS NULL)
                                                          AS rows_benes_suppressed
        FROM partd p
        JOIN cms_drug_resolution t
          ON t.brnd_name = p.brnd_name AND t.gnrc_name = p.gnrc_name
        WHERE t.molecule_id <> ''
        GROUP BY 1,2,7""")
    for ddl in (
        "CREATE INDEX t2_state_npi ON provider_molecule (state, prscrbr_npi)",
        "CREATE INDEX t2_mol ON provider_molecule (molecule_id)",
        "CREATE INDEX t2_state_mol ON provider_molecule (state, molecule_id)",
    ):
        cur.execute(ddl)
    cur.execute("ANALYZE provider_molecule")

    # ---- per-provider coverage, carried so no provider hides in an average
    cur.execute("DROP TABLE IF EXISTS provider_coverage")
    cur.execute("""
        CREATE TABLE provider_coverage AS
        SELECT p.prscrbr_state_abrvtn AS state, p.prscrbr_npi,
               sum(NULLIF(p.tot_clms,'')::double precision) AS total_clms,
               sum(CASE WHEN t.molecule_id <> ''
                        THEN NULLIF(p.tot_clms,'')::double precision ELSE 0 END)
                 AS resolved_clms,
               sum(CASE WHEN t.molecule_id = '' OR t.molecule_id IS NULL
                        THEN NULLIF(p.tot_clms,'')::double precision ELSE 0 END)
                 AS unresolved_clms
        FROM partd p
        LEFT JOIN cms_drug_resolution t
          ON t.brnd_name = p.brnd_name AND t.gnrc_name = p.gnrc_name
        GROUP BY 1,2""")
    cur.execute("CREATE INDEX pc_k ON provider_coverage (state, prscrbr_npi)")
    cur.execute("ANALYZE provider_coverage")

    cur.execute("""ALTER TABLE provider_molecule
                   ADD COLUMN provider_unresolved_clms double precision""")
    cur.execute("""UPDATE provider_molecule m
                   SET provider_unresolved_clms = c.unresolved_clms
                   FROM provider_coverage c
                   WHERE c.state = m.state AND c.prscrbr_npi = m.prscrbr_npi""")

    emit("=== T2 SHAPE, BY STATE ===")
    cur.execute("""SELECT state, count(*), count(DISTINCT prscrbr_npi),
                          count(DISTINCT molecule_id), sum(tot_clms), sum(tot_drug_cst)
                   FROM provider_molecule GROUP BY 1 ORDER BY 1""")
    emit("  %-6s %10s %11s %11s %15s %18s"
         % ("state", "T2 rows", "providers", "molecules", "claims", "cost"))
    for s, r, p, m, c, d in cur.fetchall():
        emit("  %-6s %10s %11s %11s %15s %18s"
             % (s, f"{r:,}", f"{p:,}", f"{m:,}", f"{c:,.0f}", f"${d:,.0f}"))
    cur.execute("SELECT count(*) FROM provider_molecule")
    emit("  total T2 rows: %s" % f"{cur.fetchone()[0]:,}")

    emit()
    emit("=== THE COLLAPSE: several CMS names become one molecule ===")
    cur.execute("""SELECT count(*) FILTER (WHERE n_source_pairs = 1),
                          count(*) FILTER (WHERE n_source_pairs BETWEEN 2 AND 4),
                          count(*) FILTER (WHERE n_source_pairs > 4),
                          max(n_source_pairs)
                   FROM provider_molecule""")
    a, b, c, mx = cur.fetchone()
    emit("  T2 rows built from 1 CMS pair=%s   2-4 pairs=%s   >4 pairs=%s   max=%s"
         % (f"{a:,}", f"{b:,}", f"{c:,}", mx))
    cur.execute("""SELECT state, prscrbr_npi, molecule_id, n_source_pairs, tot_clms
                   FROM provider_molecule ORDER BY n_source_pairs DESC LIMIT 5""")
    emit("  widest collapses:")
    for s, npi, mol, n, c in cur.fetchall():
        emit("    %s %s  molecule=%-16s from %s CMS names, %s claims"
             % (s, npi, mol[:16], n, f"{c:,.0f}"))

    emit()
    emit("=== PER-PROVIDER COVERAGE -- no provider hidden in an average ===")
    cur.execute("""SELECT state, count(*),
                      round(avg(100.0*resolved_clms/NULLIF(total_clms,0))::numeric,2),
                      count(*) FILTER (WHERE resolved_clms/NULLIF(total_clms,0) >= 0.99),
                      count(*) FILTER (WHERE resolved_clms/NULLIF(total_clms,0) < 0.90),
                      count(*) FILTER (WHERE resolved_clms/NULLIF(total_clms,0) < 0.60)
                   FROM provider_coverage GROUP BY 1 ORDER BY 1""")
    emit("  %-6s %11s %12s %12s %12s %12s"
         % ("state", "providers", "mean cov%", ">=99%", "<90%", "<60%"))
    for s, n, mean, hi, lo, vlo in cur.fetchall():
        emit("  %-6s %11s %11.2f%% %12s %12s %12s"
             % (s, f"{n:,}", mean, f"{hi:,}", f"{lo:,}", f"{vlo:,}"))
    emit()
    emit("  worst-covered providers carrying real volume (>=1,000 claims):")
    cur.execute("""SELECT state, prscrbr_npi, total_clms, unresolved_clms,
                          round((100.0*resolved_clms/NULLIF(total_clms,0))::numeric,1)
                   FROM provider_coverage
                   WHERE total_clms >= 1000
                   ORDER BY resolved_clms/NULLIF(total_clms,0) ASC LIMIT 10""")
    emit("    %-6s %-12s %12s %14s %8s"
         % ("state", "npi", "claims", "unresolved", "cov%"))
    for s, npi, tot, unres, cov in cur.fetchall():
        emit("    %-6s %-12s %12s %14s %7.1f%%"
             % (s, npi, f"{tot:,.0f}", f"{unres:,.0f}", cov))

    emit()
    emit("=== TOP MOLECULES BY CLAIMS, EACH STATE SEPARATELY ===")
    for st in ("DE", "AL"):
        emit("  %s:" % st)
        cur.execute("""SELECT m.molecule_id, count(DISTINCT m.prscrbr_npi),
                              sum(m.tot_clms), sum(m.tot_drug_cst),
                              min(c.gnrc_name)
                       FROM provider_molecule m
                       LEFT JOIN cms_drug_resolution c
                         ON c.molecule_id = m.molecule_id
                       WHERE m.state = %s
                       GROUP BY 1 ORDER BY 3 DESC LIMIT 8""", (st,))
        emit("    %-18s %10s %13s %16s %s"
             % ("molecule_id", "providers", "claims", "cost", "a CMS name for it"))
        for mol, np_, c, d, nm in cur.fetchall():
            emit("    %-18s %10s %13s %16s %s"
                 % (mol[:18], f"{np_:,}", f"{c:,.0f}", f"${d:,.0f}", str(nm)[:26]))

    emit()
    emit("=== SEPARATION PROOFS ===")
    cur.execute("""SELECT count(*) FROM (
                     SELECT prscrbr_npi FROM provider_molecule
                     GROUP BY prscrbr_npi HAVING count(DISTINCT state) > 1) x""")
    emit("  providers appearing in BOTH states (should be 0 if NPI is state-bound): %s"
         % cur.fetchone()[0])
    cur.execute("""SELECT count(DISTINCT molecule_id) FROM provider_molecule""")
    allm = cur.fetchone()[0]
    cur.execute("""SELECT count(*) FROM (
                     SELECT molecule_id FROM provider_molecule
                     GROUP BY molecule_id HAVING count(DISTINCT state)=2) x""")
    emit("  molecules prescribed in both states: %s of %s"
         % (f"{cur.fetchone()[0]:,}", f"{allm:,}"))
    cur.execute("""SELECT count(DISTINCT molecule_id) FROM cms_drug_resolution
                   WHERE molecule_id <> '' AND molecule_id <> match_set""")
    emit("  molecules kept distinct ONLY because identity is unfolded: %s"
         % f"{cur.fetchone()[0]:,}")
    cur.execute("""SELECT c.molecule_id, min(c.gnrc_name), sum(m.tot_clms)
                   FROM cms_drug_resolution c JOIN provider_molecule m
                     ON m.molecule_id = c.molecule_id
                   WHERE c.molecule_id <> c.match_set
                   GROUP BY 1 ORDER BY 3 DESC LIMIT 5""")
    emit("  highest-volume among those:")
    for mol, nm, c in cur.fetchall():
        emit("    %-16s %-30s %s claims" % (mol[:16], str(nm)[:30], f"{c:,.0f}"))
    con.close()
    emit()
    emit("written to " + OUT)


main()
