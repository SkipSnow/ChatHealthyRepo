"""Experiment 1 -- the BASELINE funnel, before any RxNorm intelligence.

Measures what a purely literal funnel achieves, so the algorithm that follows
has a number to beat and the gap is visible rather than asserted.

Stages, in decreasing precision. A pair is claimed by the FIRST stage that
matches it, exactly as a funnel requires, so a loose rule can never capture a
pair a stricter rule would have resolved correctly.

  1 brand_exact        CMS Brnd_Name  == FDA openfda.brand_name (case-folded)
  2 brand_normalized   brand_join_key(CMS) == brand_join_key(FDA brand)
  3 ingredient         molecule_canonical(CMS brand OR generic)
                       == molecule_canonical(FDA generic or substance)
  4 residual           nothing matched

Reported two ways, because they are different bars:
  - share of DISTINCT PAIRS resolved
  - share of CLAIM VOLUME resolved

Also reported, because resolution count alone is worthless:
  - AMBIGUITY: how many pairs match more than one distinct molecule, which is
    a wrong answer waiting to happen, not a success.

FDA rows are deduped to one per set_id: the archive holds three runs' copies.
"""
import sys
import os
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
DB = "medicare_join"
OUT = r"C:\CHATHealthyLLC\DataExperiments\exp_01_result.txt"


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    con = psycopg2.connect(host="localhost", port=5432, user="postgres",
                           password=pw, dbname=DB)
    con.autocommit = True
    cur = con.cursor()

    emit("EXPERIMENT 1 -- baseline literal funnel")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()

    # ---- the two sides, deduped -------------------------------------------
    cur.execute("DROP VIEW IF EXISTS v_fda_name CASCADE")
    cur.execute("""
        CREATE VIEW v_fda_name AS
        SELECT DISTINCT set_id, kind, value, value_norm FROM fda_name""")

    # The COMPLETE pair set from all 28,023,892 rows, carrying the
    # development / holdout split. Development = pairs seen in the first
    # 500,000 rows. Holdout = the tail, never seen while building the rules.
    cur.execute("DROP TABLE IF EXISTS exp1_pairs")
    cur.execute("""
        CREATE TABLE exp1_pairs AS
        SELECT brnd_name, gnrc_name, src_rows,
               tot_clms AS clms, tot_drug_cst AS cost, in_dev_set
        FROM partd_pair""")
    cur.execute("SELECT count(*), sum(src_rows), sum(clms) FROM exp1_pairs")
    npairs, nrows, nclms = cur.fetchone()
    emit("CMS side (complete pair set from all 28,023,892 rows)")
    emit("  distinct pairs : %s" % f"{npairs:,}")
    emit("  source rows    : %s" % f"{nrows:,}")
    emit("  total claims   : %s" % f"{nclms:,.0f}")
    cur.execute("""SELECT in_dev_set, count(*), sum(clms) FROM exp1_pairs
                   GROUP BY 1 ORDER BY 1 DESC""")
    for dev, n, c in cur.fetchall():
        emit("    %-24s pairs=%-7s claims=%s"
             % ("DEVELOPMENT" if dev else "HOLDOUT (tail)", f"{n:,}", f"{c:,.0f}"))

    cur.execute("SELECT count(DISTINCT set_id) FROM fda_label")
    emit("FDA side")
    emit("  distinct non-OTC labels : %s" % f"{cur.fetchone()[0]:,}")
    cur.execute("""SELECT kind, count(DISTINCT upper(value)), count(DISTINCT value_norm)
                   FROM v_fda_name GROUP BY kind ORDER BY kind""")
    for k, dv, dn in cur.fetchall():
        emit("  %-10s distinct_literal=%-8s distinct_normalized=%s"
             % (k, f"{dv:,}", f"{dn:,}"))
    emit()

    # ---- the funnel --------------------------------------------------------
    cur.execute("DROP TABLE IF EXISTS exp1_funnel")
    cur.execute("""
        CREATE TABLE exp1_funnel AS
        WITH b_exact AS (
          SELECT p.brnd_name, p.gnrc_name, count(DISTINCT n.set_id) AS hits
          FROM exp1_pairs p
          JOIN v_fda_name n ON n.kind='brand'
                           AND upper(n.value) = upper(p.brnd_name)
          GROUP BY 1,2
        ),
        b_norm AS (
          SELECT p.brnd_name, p.gnrc_name, count(DISTINCT n.set_id) AS hits
          FROM exp1_pairs p
          JOIN v_fda_name n ON n.kind='brand'
                           AND n.value_norm <> ''
                           AND n.value_norm = upper(regexp_replace(p.brnd_name,'[^A-Za-z0-9 ]','','g'))
          GROUP BY 1,2
        ),
        ingred AS (
          SELECT p.brnd_name, p.gnrc_name, count(DISTINCT n.set_id) AS hits
          FROM exp1_pairs p
          JOIN v_fda_name n ON n.kind IN ('generic','substance')
                           AND n.value_norm <> ''
                           AND (upper(n.value) = upper(p.gnrc_name)
                             OR upper(n.value) = upper(p.brnd_name))
          GROUP BY 1,2
        )
        SELECT p.brnd_name, p.gnrc_name, p.src_rows, p.clms, p.cost,
               p.in_dev_set,
               CASE
                 WHEN be.hits IS NOT NULL THEN '1_brand_exact'
                 WHEN bn.hits IS NOT NULL THEN '2_brand_normalized'
                 WHEN ig.hits IS NOT NULL THEN '3_ingredient'
                 ELSE '4_residual'
               END AS stage,
               COALESCE(be.hits, bn.hits, ig.hits, 0) AS label_hits
        FROM exp1_pairs p
        LEFT JOIN b_exact be USING (brnd_name, gnrc_name)
        LEFT JOIN b_norm  bn USING (brnd_name, gnrc_name)
        LEFT JOIN ingred  ig USING (brnd_name, gnrc_name)""")

    emit("FUNNEL -- pairs")
    cur.execute("""SELECT stage, count(*), round(100.0*count(*)/sum(count(*)) OVER (),2)
                   FROM exp1_funnel GROUP BY stage ORDER BY stage""")
    for s, n, pct in cur.fetchall():
        emit("  %-20s %6s pairs  %6.2f%%" % (s, f"{n:,}", pct))
    cur.execute("""SELECT round(100.0*sum(CASE WHEN stage<>'4_residual' THEN 1 ELSE 0 END)
                               /count(*),2) FROM exp1_funnel""")
    emit("  RESOLVED (pairs)     %.2f%%" % cur.fetchone()[0])
    emit()

    emit("FUNNEL -- claim volume")
    cur.execute("""SELECT stage, sum(clms), round(100.0*sum(clms)/sum(sum(clms)) OVER (),2)
                   FROM exp1_funnel GROUP BY stage ORDER BY stage""")
    for s, c, pct in cur.fetchall():
        emit("  %-20s %14s claims %6.2f%%" % (s, f"{c:,.0f}", pct))
    cur.execute("""SELECT round(100.0*sum(CASE WHEN stage<>'4_residual' THEN clms ELSE 0 END)
                               /sum(clms),2) FROM exp1_funnel""")
    emit("  RESOLVED (claims)    %.2f%%" % cur.fetchone()[0])
    emit()

    emit("AMBIGUITY -- a pair hitting many labels is a wrong answer waiting to happen")
    cur.execute("""SELECT stage,
                          count(*) FILTER (WHERE label_hits = 1),
                          count(*) FILTER (WHERE label_hits BETWEEN 2 AND 10),
                          count(*) FILTER (WHERE label_hits > 10),
                          max(label_hits)
                   FROM exp1_funnel WHERE stage <> '4_residual'
                   GROUP BY stage ORDER BY stage""")
    emit("  %-20s %8s %8s %8s %8s" % ("stage", "1 label", "2-10", ">10", "max"))
    for s, a, b, c, m in cur.fetchall():
        emit("  %-20s %8s %8s %8s %8s"
             % (s, f"{a:,}", f"{b:,}", f"{c:,}", f"{m:,}"))
    emit()

    emit("DEVELOPMENT vs HOLDOUT -- the number that actually matters")
    cur.execute("""SELECT in_dev_set,
                          round(100.0*sum(CASE WHEN stage<>'4_residual' THEN 1 ELSE 0 END)
                                /count(*),2) AS pct_pairs,
                          round(100.0*sum(CASE WHEN stage<>'4_residual' THEN clms ELSE 0 END)
                                /NULLIF(sum(clms),0),2) AS pct_claims,
                          count(*)
                   FROM exp1_funnel GROUP BY 1 ORDER BY 1 DESC""")
    emit("  %-18s %12s %12s %10s" % ("set", "pairs %", "claims %", "n"))
    for dev, pp, pc, n in cur.fetchall():
        emit("  %-18s %11.2f%% %11.2f%% %10s"
             % ("DEVELOPMENT" if dev else "HOLDOUT", pp, pc, f"{n:,}"))
    emit()

    emit("RESIDUAL -- top 40 unresolved pairs by claim volume")
    cur.execute("""SELECT brnd_name, gnrc_name, clms, cost
                   FROM exp1_funnel WHERE stage='4_residual'
                   ORDER BY clms DESC NULLS LAST LIMIT 40""")
    emit("  %-34s %-34s %12s %14s" % ("Brnd_Name", "Gnrc_Name", "claims", "cost"))
    for b, g, c, d in cur.fetchall():
        emit("  %-34s %-34s %12s %14s"
             % (str(b)[:34], str(g)[:34],
                f"{c:,.0f}" if c is not None else "-",
                f"{d:,.0f}" if d is not None else "-"))
    emit()
    cur.execute("SELECT count(*) FROM exp1_funnel WHERE stage='4_residual'")
    emit("residual pairs total: %s" % f"{cur.fetchone()[0]:,}")
    con.close()
    emit()
    emit("written to " + OUT)


main()
