"""Experiment 2 -- the baseline funnel, done correctly.

Experiment 1 was written badly: it put an OR inside a join condition and called
functions on the join columns, so no index could be used and a 3,159-row join
ran for twenty minutes. This version fixes the shape:

  1. Normalized keys are computed ONCE, in Python, using the pipeline's own
     brand_join_key and molecule_canonical -- so the experiment measures the
     same normalization production uses, not a SQL re-implementation of it.
  2. Those keys are materialised into narrow tables and INDEXED.
  3. Every funnel stage is then a plain equality join on an indexed column.
     No OR, no function calls, no regexp at join time.
  4. ANALYZE after each build so the planner has statistics.

The funnel is strictly ordered: a pair is claimed by the first stage that
matches, so a loose rule can never steal a pair a stricter rule resolves
correctly. Reported by pair count AND claim volume, development AND holdout,
with ambiguity counted -- a pair matching many distinct ingredients is a wrong
answer waiting to happen, not a success.
"""
import csv
import io
import os
import sys
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC")
sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (  # noqa: E402
    brand_join_key, molecule_canonical)

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
DB = "medicare_join"
OUT = r"C:\CHATHealthyLLC\DataExperiments\exp_02_result.txt"


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname=DB)


def build_cms_keys(con, cur):
    """One row per CMS pair, with every key form precomputed."""
    cur.execute("SELECT brnd_name, gnrc_name, src_rows, tot_clms, tot_drug_cst, "
                "in_dev_set FROM partd_pair")
    rows = cur.fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    for b, g, n, c, d, dev in rows:
        b = (b or "").strip()
        g = (g or "").strip()
        w.writerow([b, g, n, c or 0, d or 0, "true" if dev else "false",
                    b.upper(), brand_join_key(b) or "",
                    g.upper(), molecule_canonical(g) or "",
                    molecule_canonical(b) or ""])
    buf.seek(0)
    cur.execute("DROP TABLE IF EXISTS cms_key")
    cur.execute("""
        CREATE TABLE cms_key (
          brnd_name text, gnrc_name text, src_rows bigint,
          clms double precision, cost double precision, in_dev_set boolean,
          brand_upper text, brand_norm text,
          generic_upper text, generic_norm text,
          brand_as_ingredient text
        )""")
    cur.copy_expert("COPY cms_key FROM STDIN WITH (FORMAT csv)", buf)
    for ddl in (
        "CREATE INDEX cms_brand_upper ON cms_key (brand_upper)",
        "CREATE INDEX cms_brand_norm ON cms_key (brand_norm)",
        "CREATE INDEX cms_generic_upper ON cms_key (generic_upper)",
        "CREATE INDEX cms_generic_norm ON cms_key (generic_norm)",
        "CREATE INDEX cms_brand_as_ing ON cms_key (brand_as_ingredient)",
    ):
        cur.execute(ddl)
    cur.execute("ANALYZE cms_key")
    con.commit()
    cur.execute("SELECT count(*) FROM cms_key")
    emit("cms_key built and indexed: %s pairs" % f"{cur.fetchone()[0]:,}")


def build_fda_keys(con, cur):
    """Distinct FDA name keys per kind, one row per (kind, key, set_id)."""
    cur.execute("DROP TABLE IF EXISTS fda_key")
    cur.execute("""
        CREATE TABLE fda_key AS
        SELECT DISTINCT kind, upper(value) AS value_upper,
               value_norm, set_id
        FROM fda_name
        WHERE value IS NOT NULL AND value <> ''""")
    for ddl in (
        "CREATE INDEX fda_key_ku ON fda_key (kind, value_upper)",
        "CREATE INDEX fda_key_kn ON fda_key (kind, value_norm)",
        "CREATE INDEX fda_key_u ON fda_key (value_upper)",
        "CREATE INDEX fda_key_n ON fda_key (value_norm)",
    ):
        cur.execute(ddl)
    cur.execute("ANALYZE fda_key")
    con.commit()
    cur.execute("SELECT kind, count(*), count(DISTINCT value_upper), "
                "count(DISTINCT value_norm) FROM fda_key GROUP BY kind ORDER BY kind")
    emit("fda_key built and indexed:")
    for k, n, du, dn in cur.fetchall():
        emit("  %-10s rows=%-8s distinct_literal=%-8s distinct_norm=%s"
             % (k, f"{n:,}", f"{du:,}", f"{dn:,}"))


# Each stage: a name, and a single-equality join producing (pair -> set_id).
STAGES = [
    ("1_brand_exact",
     """SELECT c.brnd_name, c.gnrc_name, f.set_id
        FROM cms_key c JOIN fda_key f
          ON f.kind='brand' AND f.value_upper = c.brand_upper
        WHERE c.brand_upper <> ''"""),
    ("2_brand_normalized",
     """SELECT c.brnd_name, c.gnrc_name, f.set_id
        FROM cms_key c JOIN fda_key f
          ON f.kind='brand' AND f.value_norm = c.brand_norm
        WHERE c.brand_norm <> ''"""),
    ("3_generic_exact",
     """SELECT c.brnd_name, c.gnrc_name, f.set_id
        FROM cms_key c JOIN fda_key f
          ON f.kind IN ('generic','substance') AND f.value_upper = c.generic_upper
        WHERE c.generic_upper <> ''"""),
    ("4_generic_normalized",
     """SELECT c.brnd_name, c.gnrc_name, f.set_id
        FROM cms_key c JOIN fda_key f
          ON f.kind IN ('generic','substance') AND f.value_norm = c.generic_norm
        WHERE c.generic_norm <> ''"""),
    ("5_brand_as_ingredient",
     """SELECT c.brnd_name, c.gnrc_name, f.set_id
        FROM cms_key c JOIN fda_key f
          ON f.kind IN ('generic','substance') AND f.value_norm = c.brand_as_ingredient
        WHERE c.brand_as_ingredient <> ''"""),
]


def run_funnel(con, cur):
    cur.execute("DROP TABLE IF EXISTS funnel_hit")
    cur.execute("""CREATE TABLE funnel_hit (
                     stage text, brnd_name text, gnrc_name text, set_id text)""")
    con.commit()
    claimed = "DROP TABLE IF EXISTS claimed"
    cur.execute(claimed)
    cur.execute("CREATE TABLE claimed (brnd_name text, gnrc_name text)")
    cur.execute("CREATE INDEX claimed_k ON claimed (brnd_name, gnrc_name)")
    con.commit()

    for name, sql in STAGES:
        t0 = time.time()
        cur.execute(f"""
            INSERT INTO funnel_hit (stage, brnd_name, gnrc_name, set_id)
            SELECT '{name}', s.brnd_name, s.gnrc_name, s.set_id
            FROM ({sql}) s
            WHERE NOT EXISTS (SELECT 1 FROM claimed cl
                              WHERE cl.brnd_name = s.brnd_name
                                AND cl.gnrc_name = s.gnrc_name)""")
        inserted = cur.rowcount
        cur.execute(f"""
            INSERT INTO claimed (brnd_name, gnrc_name)
            SELECT DISTINCT brnd_name, gnrc_name FROM funnel_hit
            WHERE stage = '{name}'""")
        cur.execute("ANALYZE claimed")
        con.commit()
        cur.execute("SELECT count(*) FROM claimed")
        emit("  %-24s hits=%-9s pairs_claimed_total=%-7s %5.1fs"
             % (name, f"{inserted:,}", f"{cur.fetchone()[0]:,}", time.time() - t0))


def report(con, cur):
    emit()
    emit("FUNNEL RESULT")
    cur.execute("""
        DROP TABLE IF EXISTS funnel_pair;
        CREATE TABLE funnel_pair AS
        SELECT c.brnd_name, c.gnrc_name, c.clms, c.cost, c.in_dev_set,
               COALESCE(h.stage, '9_residual') AS stage,
               COALESCE(h.n_labels, 0) AS n_labels
        FROM cms_key c
        LEFT JOIN (SELECT brnd_name, gnrc_name, min(stage) AS stage,
                          count(DISTINCT set_id) AS n_labels
                   FROM funnel_hit GROUP BY 1,2) h
          USING (brnd_name, gnrc_name)""")
    con.commit()

    emit()
    emit("by PAIRS")
    cur.execute("""SELECT stage, count(*),
                          round((100.0*count(*)/sum(count(*)) OVER ())::numeric,2)
                   FROM funnel_pair GROUP BY stage ORDER BY stage""")
    for s, n, p in cur.fetchall():
        emit("  %-24s %6s  %6.2f%%" % (s, f"{n:,}", p))

    emit()
    emit("by CLAIM VOLUME")
    cur.execute("""SELECT stage, sum(clms),
                          round((100.0*sum(clms)/sum(sum(clms)) OVER ())::numeric,2)
                   FROM funnel_pair GROUP BY stage ORDER BY stage""")
    for s, c, p in cur.fetchall():
        emit("  %-24s %15s  %6.2f%%" % (s, f"{c:,.0f}", p))

    emit()
    emit("RESOLVED -- development vs holdout")
    cur.execute("""SELECT in_dev_set, count(*),
                      round((100.0*sum(CASE WHEN stage<>'9_residual' THEN 1 ELSE 0 END)
                            /count(*))::numeric,2),
                      round((100.0*sum(CASE WHEN stage<>'9_residual' THEN clms ELSE 0 END)
                            /NULLIF(sum(clms),0))::numeric,2)
                   FROM funnel_pair GROUP BY 1 ORDER BY 1 DESC""")
    emit("  %-16s %8s %12s %12s" % ("set", "pairs", "pairs %", "claims %"))
    for dev, n, pp, pc in cur.fetchall():
        emit("  %-16s %8s %11.2f%% %11.2f%%"
             % ("DEVELOPMENT" if dev else "HOLDOUT", f"{n:,}", pp, pc))
    cur.execute("""SELECT round((100.0*sum(CASE WHEN stage<>'9_residual' THEN 1 ELSE 0 END)
                               /count(*))::numeric,2),
                          round((100.0*sum(CASE WHEN stage<>'9_residual' THEN clms ELSE 0 END)
                               /sum(clms))::numeric,2) FROM funnel_pair""")
    pp, pc = cur.fetchone()
    emit("  %-16s %8s %11.2f%% %11.2f%%" % ("OVERALL", "3,159", pp, pc))

    emit()
    emit("AMBIGUITY -- labels matched per resolved pair")
    cur.execute("""SELECT stage,
                      count(*) FILTER (WHERE n_labels=1),
                      count(*) FILTER (WHERE n_labels BETWEEN 2 AND 10),
                      count(*) FILTER (WHERE n_labels>10), max(n_labels)
                   FROM funnel_pair WHERE stage<>'9_residual'
                   GROUP BY stage ORDER BY stage""")
    emit("  %-24s %8s %8s %8s %8s" % ("stage", "1", "2-10", ">10", "max"))
    for s, a, b, c, m in cur.fetchall():
        emit("  %-24s %8s %8s %8s %8s" % (s, f"{a:,}", f"{b:,}", f"{c:,}", f"{m:,}"))

    emit()
    emit("RESIDUAL -- top 40 by claim volume")
    cur.execute("""SELECT brnd_name, gnrc_name, clms, in_dev_set
                   FROM funnel_pair WHERE stage='9_residual'
                   ORDER BY clms DESC NULLS LAST LIMIT 40""")
    emit("  %-32s %-32s %12s %6s" % ("Brnd_Name", "Gnrc_Name", "claims", "set"))
    for b, g, c, dev in cur.fetchall():
        emit("  %-32s %-32s %12s %6s"
             % (str(b)[:32], str(g)[:32], f"{c:,.0f}" if c else "-",
                "DEV" if dev else "HOLD"))
    cur.execute("""SELECT count(*), sum(clms) FROM funnel_pair
                   WHERE stage='9_residual'""")
    n, c = cur.fetchone()
    emit()
    emit("residual: %s pairs carrying %s claims" % (f"{n:,}", f"{c:,.0f}"))


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("EXPERIMENT 2 -- baseline funnel, indexed")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()
    con = connect()
    cur = con.cursor()
    cur.execute("SET statement_timeout = '600s'")
    build_cms_keys(con, cur)
    build_fda_keys(con, cur)
    emit()
    emit("FUNNEL STAGES (each a single indexed equality join)")
    run_funnel(con, cur)
    report(con, cur)
    con.close()
    emit()
    emit("written to " + OUT)


main()
