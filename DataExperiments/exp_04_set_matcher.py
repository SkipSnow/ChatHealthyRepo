"""Experiment 4 -- the real algorithm: ingredient-SET matching.

Two failures in the baseline have one cause.

  Combination products fail because CMS writes "Hydrocodone-Acetaminophen" as
  one string while the FDA label carries substance_name as an ARRAY, and a
  string was compared to a single element.

  Klor-Con M20 "resolves" to 280 labels spanning 145 distinct ingredients
  because potassium chloride is an INGREDIENT IN hundreds of IV fluids. That is
  containment mistaken for identity.

Both dissolve under one rule: a CMS drug matches a label when their ACTIVE
INGREDIENT SETS are equal. Not when the label mentions the name somewhere.

Salt forms are the complication. CMS says "Amitriptyline Hcl"; the label says
"AMITRIPTYLINE HYDROCHLORIDE"; RxNorm calls the first PIN 203168 and relates it
to IN 704 "amitriptyline" via precise_ingredient_of. So every token on both
sides is resolved to an INGREDIENT-level RxNorm concept before the sets are
compared, which makes salt equivalence a recorded fact rather than string
surgery.

Every intermediate is materialised and inspectable: what was parsed from each
CMS name, what each token resolved to, and which labels matched with which set.
"""
import csv
import io
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
OUT = r"C:\CHATHealthyLLC\DataExperiments\exp_04_result.txt"

# CMS writes a combination's ingredients separated by one of these. The order
# matters: longest first, so " and " is not split by the bare "-" rule.
SPLIT_TOKENS = [" with ", " and ", " w/", "/", "-", "+"]

# Dose, form and packaging noise that is not an ingredient name.
NOISE = re.compile(
    r"\b("
    r"\d+(\.\d+)?\s*(mg|mcg|g|ml|l|unit|units|iu|meq|mmol|%|hr|hour)s?"
    r"|er|xr|sr|cr|dr|la|xl|ec|odt|hfa|pf|inj|iv|im|po|sl"
    r"|tablet|tablets|capsule|capsules|cap|caps|tab|tabs|solution|soln"
    r"|suspension|susp|cream|ointment|gel|patch|spray|drops|syrup|elixir"
    r"|kit|pack|pen|vial|ampul|ampule|syringe|inhaler|aerosol|powder"
    r"|oral|topical|ophthalmic|otic|nasal|rectal|vaginal|subcutaneous"
    r"|extended|release|delayed|immediate|chewable|coated|film"
    r")\b", re.I)


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def clean_token(t):
    t = t.strip().lower()
    t = NOISE.sub(" ", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def split_ingredients(name):
    """CMS name -> the set of ingredient tokens it appears to name."""
    s = " " + (name or "").lower() + " "
    parts = [s]
    for tok in SPLIT_TOKENS:
        nxt = []
        for p in parts:
            nxt.extend(p.split(tok))
        parts = nxt
    out = []
    for p in parts:
        c = clean_token(p)
        if len(c) >= 3:
            out.append(c)
    # preserve order, drop duplicates
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def build_rx_ingredient(con, cur):
    """name (cleaned) -> ingredient-level rxcui.

    Built from RxNorm: IN and MIN are already ingredient level. PIN is a salt
    form and is folded to its IN via precise_ingredient_of, so the salt and
    the base resolve to the SAME concept. Brand names are deliberately NOT
    included here -- a brand is resolved by the brand stages, and letting a
    brand名 masquerade as an ingredient is how an ingredient set gets polluted.
    """
    cur.execute("DROP TABLE IF EXISTS rx_ing")
    cur.execute("""
        CREATE TABLE rx_ing AS
        WITH base AS (
          SELECT DISTINCT c.rxcui, c.tty, upper(c.str_) AS nm
          FROM rxnconso c
          WHERE c.lat='ENG' AND c.suppress='N'
            AND c.tty IN ('IN','PIN','MIN')
            AND c.str_ IS NOT NULL AND c.str_ <> ''
        ),
        -- Folding a salt to its base ingredient. The edge is has_form, NOT
        -- precise_ingredient_of: measured on this data, has_form connects a
        -- PIN-named concept to an IN-named one 3,684 times while
        -- precise_ingredient_of does so 24 times and mostly relates a BRAND
        -- to a precise ingredient. Direction is rxcui1 = the salt form,
        -- rxcui2 = the base, verified on
        --   ('142439','has_form','5489') = hydrocodone bitartrate -> hydrocodone
        pin_to_in AS (
          SELECT DISTINCT r.rxcui1 AS pin, r.rxcui2 AS inn
          FROM rxnrel r
          JOIN (SELECT DISTINCT rxcui FROM rxnconso
                WHERE tty='IN' AND lat='ENG' AND suppress='N') i
            ON i.rxcui = r.rxcui2
          WHERE r.rela = 'has_form' AND r.rxcui1 <> r.rxcui2
        )
        SELECT b.nm,
               COALESCE(p.inn, b.rxcui) AS ing_rxcui,
               b.tty
        FROM base b
        LEFT JOIN pin_to_in p ON p.pin = b.rxcui""")
    cur.execute("CREATE INDEX rx_ing_nm ON rx_ing (nm)")
    cur.execute("ANALYZE rx_ing")
    con.commit()
    cur.execute("SELECT count(*), count(DISTINCT nm), count(DISTINCT ing_rxcui) FROM rx_ing")
    n, dn, di = cur.fetchone()
    emit("rx_ing: rows=%s distinct_names=%s distinct_ingredients=%s"
         % (f"{n:,}", f"{dn:,}", f"{di:,}"))

    # the lookup used by both sides
    cur.execute("SELECT nm, min(ing_rxcui) FROM rx_ing GROUP BY nm")
    lookup = {}
    for nm, cui in cur.fetchall():
        lookup[clean_token(nm)] = cui
    emit("rx lookup keys after cleaning: %s" % f"{len(lookup):,}")
    return lookup


def resolve_set(tokens, lookup):
    """tokens -> (frozenset of ingredient ids, how many tokens resolved)."""
    ids, unresolved = [], []
    for t in tokens:
        cui = lookup.get(t)
        if cui:
            ids.append("rx:" + str(cui))
        else:
            # keep the cleaned literal so an unknown ingredient still
            # participates in set equality rather than vanishing
            ids.append("lit:" + t)
            unresolved.append(t)
    return frozenset(ids), len(tokens) - len(unresolved)


def build_cms_sets(con, cur, lookup):
    cur.execute("""SELECT brnd_name, gnrc_name, clms, cost, in_dev_set
                   FROM cms_key""")
    buf = io.StringIO()
    w = csv.writer(buf)
    stats = {"pairs": 0, "multi": 0, "fully_resolved": 0}
    for b, g, c, d, dev in cur.fetchall():
        # the GENERIC column is the ingredient statement; the brand column is
        # only an ingredient statement when CMS put one there.
        toks = split_ingredients(g) or split_ingredients(b)
        ids, nres = resolve_set(toks, lookup)
        stats["pairs"] += 1
        if len(toks) > 1:
            stats["multi"] += 1
        if toks and nres == len(toks):
            stats["fully_resolved"] += 1
        w.writerow([b, g, c or 0, d or 0, "true" if dev else "false",
                    len(toks), "|".join(sorted(ids)), " + ".join(toks)])
    buf.seek(0)
    cur.execute("DROP TABLE IF EXISTS cms_set")
    cur.execute("""CREATE TABLE cms_set (
                     brnd_name text, gnrc_name text,
                     clms double precision, cost double precision,
                     in_dev_set boolean, n_ingredients int,
                     ing_set text, parsed text)""")
    cur.copy_expert("COPY cms_set FROM STDIN WITH (FORMAT csv)", buf)
    cur.execute("CREATE INDEX cms_set_ing ON cms_set (ing_set)")
    cur.execute("ANALYZE cms_set")
    con.commit()
    emit("cms_set: pairs=%s multi_ingredient=%s fully_resolved_to_rxnorm=%s"
         % (f"{stats['pairs']:,}", f"{stats['multi']:,}",
            f"{stats['fully_resolved']:,}"))


def build_fda_sets(con, cur, lookup):
    """Each label's ACTIVE ingredient set, from substance_name."""
    cur.execute("""SELECT set_id, array_agg(DISTINCT value) FROM fda_name
                   WHERE kind='substance' AND value <> ''
                   GROUP BY set_id""")
    rows = cur.fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    for sid, vals in rows:
        toks = []
        for v in vals:
            for t in split_ingredients(v):
                if t not in toks:
                    toks.append(t)
        ids, _ = resolve_set(toks, lookup)
        w.writerow([sid, len(toks), "|".join(sorted(ids)), " + ".join(sorted(toks))])
    buf.seek(0)
    cur.execute("DROP TABLE IF EXISTS fda_set")
    cur.execute("""CREATE TABLE fda_set (
                     set_id text, n_ingredients int, ing_set text, parsed text)""")
    cur.copy_expert("COPY fda_set FROM STDIN WITH (FORMAT csv)", buf)
    cur.execute("CREATE INDEX fda_set_ing ON fda_set (ing_set)")
    cur.execute("CREATE INDEX fda_set_sid ON fda_set (set_id)")
    cur.execute("ANALYZE fda_set")
    con.commit()
    cur.execute("SELECT count(*), count(DISTINCT ing_set) FROM fda_set")
    n, d = cur.fetchone()
    emit("fda_set: labels=%s distinct_ingredient_sets=%s" % (f"{n:,}", f"{d:,}"))


def match(con, cur):
    cur.execute("DROP TABLE IF EXISTS set_match")
    cur.execute("""
        CREATE TABLE set_match AS
        SELECT c.brnd_name, c.gnrc_name, c.clms, c.in_dev_set,
               c.n_ingredients AS cms_n, c.parsed AS cms_parsed,
               count(DISTINCT f.set_id) AS labels
        FROM cms_set c
        JOIN fda_set f ON f.ing_set = c.ing_set
        WHERE c.ing_set <> ''
        GROUP BY 1,2,3,4,5,6""")
    cur.execute("CREATE INDEX set_match_k ON set_match (brnd_name, gnrc_name)")
    cur.execute("ANALYZE set_match")
    con.commit()
    cur.execute("SELECT count(*), sum(clms) FROM set_match")
    n, c = cur.fetchone()
    emit("SET-EQUALITY matches: pairs=%s claims=%s"
         % (f"{n:,}", f"{c:,.0f}" if c else "0"))


def report(con, cur):
    emit()
    emit("=== SET MATCHING AGAINST THE BASELINE ===")
    cur.execute("""
        DROP TABLE IF EXISTS exp4_combined;
        CREATE TABLE exp4_combined AS
        SELECT p.brnd_name, p.gnrc_name, p.clms, p.in_dev_set,
               p.stage AS baseline_stage,
               (m.brnd_name IS NOT NULL) AS set_matched,
               COALESCE(m.labels,0) AS set_labels,
               COALESCE(m.cms_n,0) AS cms_n
        FROM funnel_pair p
        LEFT JOIN set_match m USING (brnd_name, gnrc_name)""")
    con.commit()

    cur.execute("""SELECT count(*), sum(clms) FROM exp4_combined""")
    allp, allc = cur.fetchone()

    emit()
    emit("What set matching recovers from the baseline RESIDUAL")
    cur.execute("""SELECT set_matched, count(*), sum(clms)
                   FROM exp4_combined WHERE baseline_stage='9_residual'
                   GROUP BY 1 ORDER BY 1 DESC""")
    for sm, n, c in cur.fetchall():
        emit("  %-14s pairs=%-6s claims=%-14s"
             % ("recovered" if sm else "still residual", f"{n:,}",
                f"{c:,.0f}" if c else "0"))

    emit()
    emit("COMBINED resolution -- baseline OR set match")
    cur.execute("""SELECT
                     round((100.0*count(*) FILTER (WHERE baseline_stage<>'9_residual'
                                                      OR set_matched)/count(*))::numeric,2),
                     round((100.0*sum(CASE WHEN baseline_stage<>'9_residual'
                                             OR set_matched THEN clms ELSE 0 END)
                           /sum(clms))::numeric,2)
                   FROM exp4_combined""")
    pp, pc = cur.fetchone()
    emit("  OVERALL   pairs=%.2f%%   claims=%.2f%%" % (pp, pc))
    cur.execute("""SELECT in_dev_set,
                     round((100.0*count(*) FILTER (WHERE baseline_stage<>'9_residual'
                                                      OR set_matched)/count(*))::numeric,2),
                     round((100.0*sum(CASE WHEN baseline_stage<>'9_residual'
                                             OR set_matched THEN clms ELSE 0 END)
                           /sum(clms))::numeric,2)
                   FROM exp4_combined GROUP BY 1 ORDER BY 1 DESC""")
    for dev, pp, pc in cur.fetchall():
        emit("  %-10s pairs=%.2f%%   claims=%.2f%%"
             % ("DEV" if dev else "HOLDOUT", pp, pc))

    emit()
    emit("PRECISION -- set matching is exact by construction; how many labels?")
    cur.execute("""SELECT
                     count(*) FILTER (WHERE set_labels=1),
                     count(*) FILTER (WHERE set_labels BETWEEN 2 AND 10),
                     count(*) FILTER (WHERE set_labels>10), max(set_labels)
                   FROM exp4_combined WHERE set_matched""")
    a, b, c, m = cur.fetchone()
    emit("  1 label=%s   2-10=%s   >10=%s   max=%s"
         % (f"{a:,}", f"{b:,}", f"{c:,}", f"{m:,}"))
    emit("  (many labels here is EXPECTED and correct: the same ingredient set")
    emit("   is marketed by many manufacturers. What matters is that every one")
    emit("   of them has the SAME ingredient set, which equality guarantees.)")

    emit()
    emit("KLOR-CON CHECK -- the containment bug")
    cur.execute("""SELECT c.brnd_name, c.gnrc_name, c.parsed,
                          (SELECT count(DISTINCT f.set_id) FROM fda_set f
                            WHERE f.ing_set = c.ing_set)
                   FROM cms_set c
                   WHERE c.brnd_name LIKE 'Klor-Con%'
                   ORDER BY 1""")
    emit("  %-16s %-24s %-28s %8s"
         % ("brand", "generic", "parsed ingredient set", "labels"))
    for b, g, p, n in cur.fetchall():
        emit("  %-16s %-24s %-28s %8s" % (str(b)[:16], str(g)[:24], str(p)[:28], n))
    emit("  baseline matched these to 280 labels spanning 145 ingredients.")

    emit()
    emit("STILL RESIDUAL -- top 30 by claims after set matching")
    cur.execute("""SELECT e.brnd_name, e.gnrc_name, e.clms, c.parsed, c.n_ingredients
                   FROM exp4_combined e
                   LEFT JOIN cms_set c USING (brnd_name, gnrc_name)
                   WHERE e.baseline_stage='9_residual' AND NOT e.set_matched
                   ORDER BY e.clms DESC NULLS LAST LIMIT 30""")
    emit("  %-30s %-28s %11s %-28s" % ("Brnd_Name", "Gnrc_Name", "claims", "parsed"))
    for b, g, c, p, n in cur.fetchall():
        emit("  %-30s %-28s %11s %-28s"
             % (str(b)[:30], str(g)[:28], f"{c:,.0f}" if c else "-", str(p)[:28]))
    con.close()
    emit()
    emit("written to " + OUT)


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("EXPERIMENT 4 -- ingredient-SET matching with RxNorm salt folding")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()
    con = connect()
    cur = con.cursor()
    cur.execute("SET statement_timeout = '900s'")
    lookup = build_rx_ingredient(con, cur)
    build_cms_sets(con, cur, lookup)
    build_fda_sets(con, cur, lookup)
    match(con, cur)
    report(con, cur)


main()
