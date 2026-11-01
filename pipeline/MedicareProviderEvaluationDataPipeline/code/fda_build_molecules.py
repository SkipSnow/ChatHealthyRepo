"""The FDA molecule universe: every FDA label resolved to its molecule.

THE COLLECTION IS COMPLETE OVER THE FDA, which means its molecules come from
the FDA, not from Medicare. The previous build reached its molecules through
cms_drug_resolution -- the Part D drug-name bridge, scoped to the drug names
Delaware and Alabama happened to bill. A collection that is supposed to carry
every FDA product was being decided by two states' prescribing.

fda_mol is built from fda_name (openFDA substances) and the RxNorm vocabulary
and touches no CMS table. The resolution functions are imported from
exec_02_build_t1 rather than copied, so the fold/identity logic has one home.

BR-016 holds here as it does everywhere: the MATCH key folds salt to base, the
IDENTITY key does not. Both are written, and the difference between them is
the formulation distinction a fold would erase.
"""
import csv
import io
import os
import sys
import time

sys.path.insert(0, r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline\code")

import psycopg2

from exec_02_build_t1 import ARTIFACTS, load_vocab, make_extend, resolve, tokens

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline\code\fda_build_molecules_result.txt"


def emit(m=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(m + "\n")
    _log.info("%s", m)


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("THE FDA MOLECULE UNIVERSE -- every FDA label, no CMS input")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    con = psycopg2.connect(host="localhost", port=5432, user="postgres",
                           password=pw, dbname="medicare_join")
    con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='0'")

    # ---- rx_ing, the salt-fold ingredient vocabulary ----------------------
    emit("=== rx_ing (BR-016 has_form fold) ===")
    cur.execute("DROP TABLE IF EXISTS rx_ing")
    cur.execute("""
        CREATE TABLE rx_ing AS
        WITH base AS (
          SELECT DISTINCT c.rxcui, c.tty, upper(c.str_) AS nm
          FROM rxnconso c
          WHERE c.lat='ENG' AND c.suppress='N'
            AND c.tty IN ('IN','PIN','MIN')
            AND c.str_ IS NOT NULL AND c.str_ <> ''),
        pin_to_in AS (
          SELECT DISTINCT r.rxcui1 AS pin, r.rxcui2 AS inn
          FROM rxnrel r
          JOIN (SELECT DISTINCT rxcui FROM rxnconso
                WHERE tty='IN' AND lat='ENG' AND suppress='N') i
            ON i.rxcui = r.rxcui2
          WHERE r.rela = 'has_form' AND r.rxcui1 <> r.rxcui2)
        SELECT b.nm, COALESCE(p.inn, b.rxcui) AS ing_rxcui, b.tty
        FROM base b LEFT JOIN pin_to_in p ON p.pin = b.rxcui""")
    cur.execute("CREATE INDEX rx_ing_nm ON rx_ing (nm)")
    cur.execute("ANALYZE rx_ing")
    cur.execute("SELECT count(*) FROM rx_ing")
    emit("   rx_ing: %s rows" % f"{cur.fetchone()[0]:,}")

    # ---- ct_bridge_t1, the trials alias graph -----------------------------
    # A trial names its intervention and lists that intervention's other
    # names. Each such pair is an alias edge, and an edge with one end in the
    # RxNorm ingredient vocabulary carries the other end into it. An alias
    # reaching more than one ingredient is AMBIGUOUS and is rejected, not
    # guessed at: n_ing is kept so the rejection is countable.
    emit()
    emit("=== ct_bridge_t1 (the trials alias graph) ===")
    cur.execute("DROP TABLE IF EXISTS ct_bridge_t1")
    cur.execute("""
        CREATE TABLE ct_bridge_t1 AS
        WITH edges AS (
          SELECT DISTINCT upper(name) AS nm, upper(other_name) AS alias
          FROM ct_intervention
          WHERE name <> '' AND other_name <> ''
            AND upper(name) <> upper(other_name)
            AND NOT (upper(name) = ANY(%(a)s))
            AND NOT (upper(other_name) = ANY(%(a)s))
            AND upper(name) NOT LIKE '%%PLACEBO%%'
            AND upper(other_name) NOT LIKE '%%PLACEBO%%'
            AND upper(name) NOT LIKE '%%SHAM%%'
            AND upper(other_name) NOT LIKE '%%SHAM%%'),
        ing AS (SELECT DISTINCT nm, ing_rxcui FROM rx_ing),
        edge_both AS (
          SELECT e.nm AS src, i.ing_rxcui FROM edges e JOIN ing i ON i.nm = e.alias
          UNION
          SELECT e.alias AS src, i.ing_rxcui FROM edges e JOIN ing i ON i.nm = e.nm)
        SELECT src, min(ing_rxcui) AS ing_rxcui,
               count(DISTINCT ing_rxcui) AS n_ing
        FROM edge_both GROUP BY src
    """, {"a": sorted(ARTIFACTS)})
    cur.execute("CREATE INDEX cbt_src ON ct_bridge_t1 (src)")
    cur.execute("ANALYZE ct_bridge_t1")
    cur.execute("""SELECT count(*) FILTER (WHERE n_ing = 1),
                          count(*) FILTER (WHERE n_ing > 1) FROM ct_bridge_t1""")
    uniq, amb = cur.fetchone()
    emit("   aliases reaching exactly one ingredient: %s" % f"{uniq:,}")
    emit("   aliases reaching several, rejected:      %s" % f"{amb:,}")

    # ---- the vocabulary, then every label's molecule ----------------------
    emit()
    emit("=== vocabulary ===")
    precise, fold, single = load_vocab(cur)
    extend = make_extend(single)

    emit()
    emit("=== fda_mol: one row per FDA label ===")
    cur.execute("""SELECT set_id, array_agg(DISTINCT value) FROM fda_name
                   WHERE kind='substance' AND value <> '' GROUP BY set_id""")
    rows = cur.fetchall()
    emit("   labels carrying a substance: %s" % f"{len(rows):,}")
    buf = io.StringIO(); w = csv.writer(buf)
    for sid, vals in rows:
        toks, seen = [], set()
        for v in vals:
            for t in tokens(v):
                if t not in seen:
                    seen.add(t); toks.append(t)
        ident, match, _ = resolve(toks, precise, fold, extend)
        w.writerow([sid, ident, match, " + ".join(sorted(toks)), "substance"])
    buf.seek(0)
    cur.execute("DROP TABLE IF EXISTS fda_mol")
    cur.execute("""CREATE TABLE fda_mol (set_id text, ident_set text,
                                         match_set text, parsed text,
                                         route text)""")
    cur.copy_expert("COPY fda_mol (set_id, ident_set, match_set, parsed, route) "
                    "FROM STDIN WITH (FORMAT csv)", buf)
    cur.execute("CREATE INDEX fm_match ON fda_mol (match_set)")
    cur.execute("CREATE INDEX fm_ident ON fda_mol (ident_set)")
    cur.execute("CREATE INDEX fm_set ON fda_mol (set_id)")
    cur.execute("ANALYZE fda_mol")
    cur.execute("""SELECT count(*), count(DISTINCT match_set),
                          count(DISTINCT ident_set) FROM fda_mol""")
    a, b_, c_ = cur.fetchone()
    emit("   fda_mol labels=%s  distinct match sets=%s  distinct identity sets=%s"
         % (f"{a:,}", f"{b_:,}", f"{c_:,}"))

    # ---- the trials rung ---------------------------------------------------
    # A label the substance route could not place is tried against the trials
    # alias graph by its brand and generic names. Two populations reach here:
    # a label whose substance names resolved to nothing, and a label carrying
    # no substance at all, which never entered fda_mol. Both are molecules the
    # FDA data alone cannot identify, so the route is recorded on the row --
    # the collection should be able to say which of its molecules it owes to
    # the trials corpus rather than to the FDA.
    emit()
    emit("=== the trials rung: labels the substance route could not place ===")
    cur.execute("""SELECT count(*) FROM fda_mol WHERE ident_set NOT LIKE '%rx:%'""")
    emit("   labels in fda_mol with no RxNorm identity: %s" % f"{cur.fetchone()[0]:,}")
    cur.execute("""
        SELECT count(DISTINCT l.set_id) FROM fda_label l
        WHERE NOT EXISTS (SELECT 1 FROM fda_mol m WHERE m.set_id = l.set_id)""")
    emit("   labels absent from fda_mol entirely:       %s" % f"{cur.fetchone()[0]:,}")
    cur.execute("""
        CREATE TEMP TABLE ct_hit AS
        SELECT DISTINCT l.set_id, 'rx:' || c.ing_rxcui::text AS mol
        FROM fda_label l
        JOIN fda_name n ON n.set_id = l.set_id
                       AND n.kind IN ('brand', 'generic') AND n.value <> ''
        JOIN ct_bridge_t1 c ON c.src = upper(n.value) AND c.n_ing = 1
        WHERE NOT EXISTS (SELECT 1 FROM fda_mol m
                          WHERE m.set_id = l.set_id AND m.ident_set LIKE '%rx:%')""")
    # A label whose brand and generic names reach DIFFERENT ingredients is as
    # ambiguous as an alias that does; it is dropped for the same reason.
    cur.execute("""DELETE FROM ct_hit WHERE set_id IN
                   (SELECT set_id FROM ct_hit GROUP BY set_id HAVING count(*) > 1)""")
    cur.execute("SELECT count(*) FROM ct_hit")
    emit("   labels the trials graph places unambiguously: %s" % f"{cur.fetchone()[0]:,}")
    cur.execute("""
        UPDATE fda_mol m SET ident_set = h.mol, match_set = h.mol,
                             route = 'clinical_trial'
        FROM ct_hit h WHERE h.set_id = m.set_id""")
    emit("   existing fda_mol rows given an identity: %s" % cur.rowcount)
    cur.execute("""
        INSERT INTO fda_mol (set_id, ident_set, match_set, parsed, route)
        SELECT h.set_id, h.mol, h.mol, '', 'clinical_trial'
        FROM ct_hit h
        WHERE NOT EXISTS (SELECT 1 FROM fda_mol m WHERE m.set_id = h.set_id)""")
    emit("   labels added to fda_mol: %s" % cur.rowcount)
    cur.execute("ANALYZE fda_mol")
    cur.execute("""SELECT route, count(*), count(DISTINCT ident_set)
                   FROM fda_mol WHERE ident_set <> '' GROUP BY 1 ORDER BY 2 DESC""")
    emit("   identified labels and molecules by route:")
    for r, nl, nm in cur.fetchall():
        emit("      %-18s %8s labels  %6s molecules" % (r, f"{nl:,}", f"{nm:,}"))
    cur.execute("""SELECT count(DISTINCT ident_set) FROM fda_mol
                   WHERE ident_set <> ''""")
    emit("   distinct molecules after the trials rung: %s" % f"{cur.fetchone()[0]:,}")

    # ---- brand lookups, from fda_name only --------------------------------
    cur.execute("DROP TABLE IF EXISTS fda_brand_norm")
    cur.execute("""CREATE TABLE fda_brand_norm AS
                   SELECT DISTINCT value_norm AS nm, set_id FROM fda_name
                   WHERE kind='brand' AND value_norm <> ''""")
    cur.execute("CREATE INDEX fbn_nm ON fda_brand_norm (nm)")
    cur.execute("ANALYZE fda_brand_norm")
    cur.execute("SELECT count(*) FROM fda_brand_norm")
    emit("   fda_brand_norm: %s rows" % f"{cur.fetchone()[0]:,}")

    # ---- THE SIZE OF THE INFERENCE THIS IMPLIES ---------------------------
    # The molecule is the delivered grain, so this is the count that sets the
    # cost of the crosswalk stage. It is reported, never capped here.
    emit()
    emit("=== the inference this collection implies ===")
    cur.execute("""
        SELECT count(DISTINCT f.match_set)
        FROM fda_mol f
        JOIN fda_label l ON l.set_id = f.set_id
        WHERE btrim(coalesce(l.indications_and_usage,'')) <> ''""")
    emit("   molecules with indication prose: %s" % f"{cur.fetchone()[0]:,}")
    cur.execute("""
        SELECT count(DISTINCT f.set_id)
        FROM fda_mol f
        JOIN fda_label l ON l.set_id = f.set_id
        WHERE btrim(coalesce(l.indications_and_usage,'')) <> ''""")
    emit("   labels behind them: %s" % f"{cur.fetchone()[0]:,}")
    con.close()
    emit()
    emit("written to " + OUT)


main()
