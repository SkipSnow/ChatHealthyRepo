"""Experiment 6 -- prove or rule out ClinicalTrials.gov as a molecule bridge.

The claim to test: a trial record names its intervention by the sponsor's name
AND lists otherNames as aliases, so an unresolved CMS brand can be walked
across that alias edge to a name RxNorm does know.

    CMS "Kerendia"  ->  trial intervention "Kerendia"
                     ->  otherNames ["finerenone", "BAY 94-8862"]
                     ->  RxNorm ingredient finerenone

This is a BRIDGE test, not a resolution attempt: the question is only whether
the registry supplies an alias that RxNorm recognises, for the pairs nothing
else resolved.

It is measured against the populations that are actually failing:
  - the 487 pairs still residual after set matching and prefix extension
  - the 1,679 BRAND pairs (90.05% resolved, worst of the known kinds)
  - the 670 pairs RxNorm has never heard of (53.73% resolved)

A bridge that recovers nothing gets ruled out and said so plainly. Only
single-ingredient aliases are accepted, and an alias resolving to more than
one ingredient is rejected rather than guessed.
"""
import os
import re
import sys
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC")
sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\exp_06_result.txt"

NOISE = re.compile(
    r"\b(\d+(\.\d+)?\s*(mg|mcg|g|ml|unit|units|iu|meq|%)s?"
    r"|placebo|tablet|tablets|capsule|capsules|injection|solution|oral|iv|sc"
    r"|arm|group|cohort|dose|doses|daily|bid|tid|qd|weekly)\b", re.I)


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
    emit("EXPERIMENT 6 -- ClinicalTrials.gov as a molecule bridge")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()

    con = connect()
    con.autocommit = True
    cur = con.cursor()
    cur.execute("SET statement_timeout='900s'")

    cur.execute("SELECT count(*), count(DISTINCT nct_id) FROM ct_intervention")
    n, d = cur.fetchone()
    emit("trials loaded: intervention rows=%s studies=%s" % (f"{n:,}", f"{d:,}"))

    # --- the alias graph: a name and every other name on the same intervention
    cur.execute("""
        DROP TABLE IF EXISTS ct_alias;
        CREATE TABLE ct_alias AS
        SELECT DISTINCT upper(a.name) AS nm, upper(a.other_name) AS alias
        FROM ct_intervention a
        WHERE a.name <> '' AND a.other_name <> ''
          AND upper(a.name) <> upper(a.other_name);
        CREATE INDEX ct_alias_nm ON ct_alias (nm);
        CREATE INDEX ct_alias_al ON ct_alias (alias);
    """)
    cur.execute("SELECT count(*), count(DISTINCT nm) FROM ct_alias")
    n, d = cur.fetchone()
    emit("alias edges=%s distinct_named_interventions=%s" % (f"{n:,}", f"{d:,}"))

    # --- can an alias reach an RxNorm INGREDIENT?
    cur.execute("""
        DROP TABLE IF EXISTS ct_bridge;
        CREATE TABLE ct_bridge AS
        WITH ing AS (
          SELECT DISTINCT nm, ing_rxcui FROM rx_ing
        )
        -- forward: CMS name matches a trial intervention, its ALIAS is an ingredient
        SELECT a.nm AS cms_side, a.alias AS bridged_name, i.ing_rxcui
        FROM ct_alias a JOIN ing i ON i.nm = a.alias
        UNION
        -- reverse: CMS name matches an ALIAS, the intervention NAME is an ingredient
        SELECT a.alias AS cms_side, a.nm AS bridged_name, i.ing_rxcui
        FROM ct_alias a JOIN ing i ON i.nm = a.nm;
        CREATE INDEX ct_bridge_k ON ct_bridge (cms_side);
    """)
    cur.execute("""SELECT count(*), count(DISTINCT cms_side),
                          count(DISTINCT ing_rxcui) FROM ct_bridge""")
    a, b, c = cur.fetchone()
    emit("bridge edges=%s distinct source names=%s distinct ingredients=%s"
         % (f"{a:,}", f"{b:,}", f"{c:,}"))

    # --- only accept names bridging to exactly ONE ingredient
    cur.execute("""
        DROP TABLE IF EXISTS ct_bridge_unique;
        CREATE TABLE ct_bridge_unique AS
        SELECT cms_side, min(ing_rxcui) AS ing_rxcui, count(DISTINCT ing_rxcui) AS n_ing
        FROM ct_bridge GROUP BY cms_side;
        CREATE INDEX ctbu ON ct_bridge_unique (cms_side);
    """)
    cur.execute("""SELECT count(*) FILTER (WHERE n_ing=1), count(*) FILTER (WHERE n_ing>1)
                   FROM ct_bridge_unique""")
    u, m = cur.fetchone()
    emit("names bridging to exactly one ingredient=%s   ambiguous (rejected)=%s"
         % (f"{u:,}", f"{m:,}"))
    emit()

    # ---------- THE TEST ----------
    emit("=== DOES IT RECOVER THE RESIDUAL? ===")
    cur.execute("""
        DROP TABLE IF EXISTS exp6_test;
        CREATE TABLE exp6_test AS
        SELECT e.brnd_name, e.gnrc_name, e.clms, e.in_dev_set,
               (e.baseline_stage <> '9_residual' OR e.set_matched) AS already_resolved,
               bb.ing_rxcui AS bridge_from_brand,
               bg.ing_rxcui AS bridge_from_generic
        FROM exp5_combined e
        LEFT JOIN ct_bridge_unique bb
               ON bb.cms_side = upper(e.brnd_name) AND bb.n_ing = 1
        LEFT JOIN ct_bridge_unique bg
               ON bg.cms_side = upper(e.gnrc_name) AND bg.n_ing = 1;
    """)
    cur.execute("""SELECT count(*), sum(clms) FROM exp6_test
                   WHERE NOT already_resolved""")
    rn, rc = cur.fetchone()
    emit("unresolved before the bridge: %s pairs / %s claims"
         % (f"{rn:,}", f"{rc:,.0f}"))

    cur.execute("""SELECT count(*), sum(clms) FROM exp6_test
                   WHERE NOT already_resolved
                     AND (bridge_from_brand IS NOT NULL
                       OR bridge_from_generic IS NOT NULL)""")
    gn, gc = cur.fetchone()
    emit("RECOVERED BY THE BRIDGE:      %s pairs / %s claims"
         % (f"{gn:,}", f"{(gc or 0):,.0f}"))
    if rn:
        emit("  that is %.1f%% of the residual pairs and %.1f%% of residual claims"
             % (100.0 * (gn or 0) / rn, 100.0 * (gc or 0) / (rc or 1)))

    cur.execute("""SELECT
                     round((100.0*count(*) FILTER (WHERE already_resolved
                              OR bridge_from_brand IS NOT NULL
                              OR bridge_from_generic IS NOT NULL)/count(*))::numeric,2),
                     round((100.0*sum(CASE WHEN already_resolved
                              OR bridge_from_brand IS NOT NULL
                              OR bridge_from_generic IS NOT NULL
                              THEN clms ELSE 0 END)/sum(clms))::numeric,2)
                   FROM exp6_test""")
    pp, pc = cur.fetchone()
    emit()
    emit("TOTAL WITH THE BRIDGE:  pairs=%.2f%%   claims=%.2f%%" % (pp, pc))
    emit("  (without it: pairs=84.58%   claims=97.68%)")

    cur.execute("""SELECT in_dev_set,
                     round((100.0*count(*) FILTER (WHERE already_resolved
                              OR bridge_from_brand IS NOT NULL
                              OR bridge_from_generic IS NOT NULL)/count(*))::numeric,2),
                     round((100.0*sum(CASE WHEN already_resolved
                              OR bridge_from_brand IS NOT NULL
                              OR bridge_from_generic IS NOT NULL
                              THEN clms ELSE 0 END)/sum(clms))::numeric,2)
                   FROM exp6_test GROUP BY 1 ORDER BY 1 DESC""")
    for dev, pp, pc in cur.fetchall():
        emit("  %-9s pairs=%.2f%%   claims=%.2f%%"
             % ("DEV" if dev else "HOLDOUT", pp, pc))

    emit()
    emit("WHAT IT RECOVERED -- top 30 by claims")
    cur.execute("""SELECT t.brnd_name, t.gnrc_name, t.clms,
                          COALESCE(t.bridge_from_brand, t.bridge_from_generic)
                   FROM exp6_test t
                   WHERE NOT t.already_resolved
                     AND (t.bridge_from_brand IS NOT NULL
                       OR t.bridge_from_generic IS NOT NULL)
                   ORDER BY t.clms DESC NULLS LAST LIMIT 30""")
    rows = cur.fetchall()
    if not rows:
        emit("  NOTHING. The bridge recovers no residual pair.")
    else:
        emit("  %-32s %-30s %11s %-26s"
             % ("Brnd_Name", "Gnrc_Name", "claims", "bridged ingredient"))
        for b, g, c, cui in rows:
            cur.execute("SELECT nm FROM rx_ing WHERE ing_rxcui=%s "
                        "ORDER BY length(nm) LIMIT 1", (cui,))
            r = cur.fetchone()
            emit("  %-32s %-30s %11s %-26s"
                 % (str(b)[:32], str(g)[:30], f"{c:,.0f}" if c else "-",
                    (r[0] if r else str(cui))[:26]))

    emit()
    emit("SPOT CHECK -- the brands that motivated this")
    for probe in ("KERENDIA", "UBRELVY", "SHINGREX", "SHINGRIX", "BREZTRI AEROSPHERE"):
        cur.execute("""SELECT bridged_name, ing_rxcui FROM ct_bridge
                       WHERE cms_side=%s LIMIT 4""", (probe,))
        hits = cur.fetchall()
        cur.execute("""SELECT count(*) FROM ct_intervention
                       WHERE upper(name)=%s OR upper(other_name)=%s""", (probe, probe))
        seen = cur.fetchone()[0]
        emit("  %-20s trial rows=%-6s bridge hits=%s %s"
             % (probe, seen, len(hits), hits[:2] if hits else ""))
    con.close()
    emit()
    emit("written to " + OUT)


main()
