"""Experiment 5 -- resolve CMS's TRUNCATED ingredient names.

CMS truncates drug names to fit a fixed-width column, so the ingredient
statement arrives cut off mid-word:

    Amoxicillin/Potassium Clav      clavulanate
    Fluticasone Propion/Salmeter    propionate, salmeterol
    Butalb/Acetaminophen/Caffein    butalbital, caffeine
    Nitrofurantoin Monohyd/M-Cry    monohydrate, macrocrystals

None of these is a fuzzy match problem. Each truncation is a PREFIX of a real
RxNorm ingredient name, so it can be extended deterministically:

    extend(token) = the unique RxNorm ingredient whose name starts with token

UNIQUE is the whole discipline. "fluticasone propion" extends to exactly one
ingredient and is accepted. A prefix matching several different ingredients is
REJECTED, not guessed -- guessing is how "active ingredients:" matched inside
"inactive ingredients:" and keyed 244 labels to talc.

A minimum token length is enforced for the same reason: a three-character
prefix would extend to dozens of unrelated ingredients.

Everything is recorded: for each extension, the token, the ingredient chosen,
and how many candidates it had.
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
OUT = r"C:\CHATHealthyLLC\DataExperiments\exp_05_result.txt"

MIN_PREFIX = 6          # shorter than this extends to too many ingredients
SPLIT_TOKENS = [" with ", " and ", " w/", "/", "-", "+"]

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
    return re.sub(r"\s+", " ", t).strip()


def split_ingredients(name):
    s = " " + (name or "").lower() + " "
    parts = [s]
    for tok in SPLIT_TOKENS:
        nxt = []
        for p in parts:
            nxt.extend(p.split(tok))
        parts = nxt
    out, seen = [], set()
    for p in parts:
        c = clean_token(p)
        if len(c) >= 3 and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def load_rx(cur):
    """exact name -> ingredient id, and the sorted name list for prefixing.

    The prefix list is restricted to SINGLE-INGREDIENT RxNorm names. Without
    that restriction "hydrochlorothiazid" reports 28 candidate ingredients --
    but they are 28 COMBINATION names that merely begin with it
    ("hydrochlorothiazide / lisinopril" and so on), not 28 ingredients. The
    truncation names one ingredient, so only one-ingredient names may answer
    it. This both recovers real matches and removes a class of false ones.
    """
    cur.execute("SELECT nm, min(ing_rxcui) FROM rx_ing GROUP BY nm")
    exact, names, skipped = {}, [], 0
    for nm, cui in cur.fetchall():
        c = clean_token(nm)
        if not c:
            continue
        exact.setdefault(c, cui)
        # a combination name carries a separator; such a name can never be the
        # unique completion of a single truncated ingredient
        if any(sep in nm for sep in ("/", " and ", " AND ", "+", " with ", " WITH ")):
            skipped += 1
            continue
        names.append((c, cui))
    names.sort()
    emit("rx exact keys=%s  single-ingredient prefix list=%s  (combination names excluded: %s)"
         % (f"{len(exact):,}", f"{len(names):,}", f"{skipped:,}"))
    return exact, names


def build_prefix_index(names):
    """token -> set of ingredient ids whose name starts with it.

    Built only for prefixes of length MIN_PREFIX and above, so the index stays
    small and short prefixes can never resolve.
    """
    import bisect
    keys = [n for n, _ in names]

    def extend(tok):
        """Return (ingredient_id, n_candidates). id is None unless unique."""
        if len(tok) < MIN_PREFIX:
            return None, 0
        i = bisect.bisect_left(keys, tok)
        cands = set()
        j = i
        while j < len(keys) and keys[j].startswith(tok):
            cands.add(names[j][1])
            j += 1
            if len(cands) > 1 and j - i > 200:
                break
        if len(cands) == 1:
            return next(iter(cands)), 1
        return None, len(cands)

    return extend


def resolve(tokens, exact, extend, audit, side):
    ids = []
    for t in tokens:
        cui = exact.get(t)
        if cui:
            ids.append("rx:" + str(cui))
            continue
        cui, n = extend(t)
        if cui:
            ids.append("rx:" + str(cui))
            audit.append((side, t, "prefix_unique", str(cui), n))
        else:
            ids.append("lit:" + t)
            audit.append((side, t, "unresolved", "", n))
    return frozenset(ids)


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("EXPERIMENT 5 -- unique-prefix extension for CMS truncation")
    emit("run at %s   MIN_PREFIX=%d" % (time.strftime("%Y-%m-%d %H:%M:%S"), MIN_PREFIX))
    emit()

    con = connect()
    cur = con.cursor()
    cur.execute("SET statement_timeout = '900s'")
    exact, names = load_rx(cur)
    extend = build_prefix_index(names)
    audit = []

    # ---- CMS side ----
    cur.execute("SELECT brnd_name, gnrc_name, clms, cost, in_dev_set FROM cms_key")
    buf = io.StringIO()
    w = csv.writer(buf)
    n_multi = 0
    for b, g, c, d, dev in cur.fetchall():
        toks = split_ingredients(g) or split_ingredients(b)
        if len(toks) > 1:
            n_multi += 1
        ids = resolve(toks, exact, extend, audit, "cms")
        w.writerow([b, g, c or 0, d or 0, "true" if dev else "false",
                    len(toks), "|".join(sorted(ids)), " + ".join(toks)])
    buf.seek(0)
    cur.execute("DROP TABLE IF EXISTS cms_set5")
    cur.execute("""CREATE TABLE cms_set5 (
                     brnd_name text, gnrc_name text, clms double precision,
                     cost double precision, in_dev_set boolean,
                     n_ingredients int, ing_set text, parsed text)""")
    cur.copy_expert("COPY cms_set5 FROM STDIN WITH (FORMAT csv)", buf)
    cur.execute("CREATE INDEX cms_set5_ing ON cms_set5 (ing_set)")
    con.commit()
    emit("cms_set5 built: multi_ingredient=%s" % f"{n_multi:,}")

    # ---- FDA side ----
    cur.execute("""SELECT set_id, array_agg(DISTINCT value) FROM fda_name
                   WHERE kind='substance' AND value <> '' GROUP BY set_id""")
    buf = io.StringIO()
    w = csv.writer(buf)
    for sid, vals in cur.fetchall():
        toks, seen = [], set()
        for v in vals:
            for t in split_ingredients(v):
                if t not in seen:
                    seen.add(t)
                    toks.append(t)
        ids = resolve(toks, exact, extend, audit, "fda")
        w.writerow([sid, len(toks), "|".join(sorted(ids)), " + ".join(sorted(toks))])
    buf.seek(0)
    cur.execute("DROP TABLE IF EXISTS fda_set5")
    cur.execute("""CREATE TABLE fda_set5 (
                     set_id text, n_ingredients int, ing_set text, parsed text)""")
    cur.copy_expert("COPY fda_set5 FROM STDIN WITH (FORMAT csv)", buf)
    cur.execute("CREATE INDEX fda_set5_ing ON fda_set5 (ing_set)")
    con.commit()
    cur.execute("SELECT count(*), count(DISTINCT ing_set) FROM fda_set5")
    emit("fda_set5 built: labels=%s distinct_sets=%s"
         % tuple(f"{x:,}" for x in cur.fetchone()))

    # ---- the extension audit ----
    buf = io.StringIO()
    w = csv.writer(buf)
    for row in audit:
        w.writerow(row)
    buf.seek(0)
    cur.execute("DROP TABLE IF EXISTS prefix_audit")
    cur.execute("""CREATE TABLE prefix_audit (
                     side text, token text, outcome text,
                     ing_rxcui text, n_candidates int)""")
    cur.copy_expert("COPY prefix_audit FROM STDIN WITH (FORMAT csv)", buf)
    con.commit()
    emit()
    emit("PREFIX EXTENSION AUDIT")
    cur.execute("""SELECT side, outcome, count(*), count(DISTINCT token)
                   FROM prefix_audit GROUP BY 1,2 ORDER BY 1,2""")
    for s, o, n, d in cur.fetchall():
        emit("  %-4s %-16s occurrences=%-8s distinct_tokens=%s"
             % (s, o, f"{n:,}", f"{d:,}"))
    emit()
    emit("  extensions actually used (CMS side, top 25 distinct tokens)")
    cur.execute("""SELECT token, ing_rxcui, count(*) FROM prefix_audit
                   WHERE side='cms' AND outcome='prefix_unique'
                   GROUP BY 1,2 ORDER BY 3 DESC LIMIT 25""")
    for t, cui, n in cur.fetchall():
        cur.execute("SELECT nm FROM rx_ing WHERE ing_rxcui=%s ORDER BY length(nm) LIMIT 1", (cui,))
        r = cur.fetchone()
        emit("     %-28s -> %-32s (%s uses)" % (t, (r[0] if r else cui)[:32], n))
    emit()
    emit("  rejected as AMBIGUOUS (top 20 by candidate count)")
    cur.execute("""SELECT token, max(n_candidates), count(*) FROM prefix_audit
                   WHERE outcome='unresolved' AND n_candidates > 1
                   GROUP BY 1 ORDER BY 2 DESC LIMIT 20""")
    for t, nc, n in cur.fetchall():
        emit("     %-28s %s candidate ingredients" % (t, nc))

    # ---- match and measure ----
    cur.execute("DROP TABLE IF EXISTS set_match5")
    cur.execute("""
        CREATE TABLE set_match5 AS
        SELECT c.brnd_name, c.gnrc_name, count(DISTINCT f.set_id) AS labels
        FROM cms_set5 c JOIN fda_set5 f ON f.ing_set = c.ing_set
        WHERE c.ing_set <> '' GROUP BY 1,2""")
    cur.execute("CREATE INDEX set_match5_k ON set_match5 (brnd_name, gnrc_name)")
    con.commit()

    cur.execute("""
        DROP TABLE IF EXISTS exp5_combined;
        CREATE TABLE exp5_combined AS
        SELECT p.brnd_name, p.gnrc_name, p.clms, p.in_dev_set,
               p.stage AS baseline_stage,
               (m.brnd_name IS NOT NULL) AS set_matched,
               COALESCE(m.labels,0) AS labels
        FROM funnel_pair p
        LEFT JOIN set_match5 m USING (brnd_name, gnrc_name)""")
    con.commit()

    emit()
    emit("=== RESULT ===")
    cur.execute("""SELECT
                     round((100.0*count(*) FILTER (WHERE baseline_stage<>'9_residual'
                                                      OR set_matched)/count(*))::numeric,2),
                     round((100.0*sum(CASE WHEN baseline_stage<>'9_residual'
                                             OR set_matched THEN clms ELSE 0 END)
                           /sum(clms))::numeric,2)
                   FROM exp5_combined""")
    pp, pc = cur.fetchone()
    emit("  OVERALL   pairs=%.2f%%   claims=%.2f%%" % (pp, pc))
    cur.execute("""SELECT in_dev_set,
                     round((100.0*count(*) FILTER (WHERE baseline_stage<>'9_residual'
                                                      OR set_matched)/count(*))::numeric,2),
                     round((100.0*sum(CASE WHEN baseline_stage<>'9_residual'
                                             OR set_matched THEN clms ELSE 0 END)
                           /sum(clms))::numeric,2)
                   FROM exp5_combined GROUP BY 1 ORDER BY 1 DESC""")
    for dev, pp, pc in cur.fetchall():
        emit("  %-9s pairs=%.2f%%   claims=%.2f%%"
             % ("DEV" if dev else "HOLDOUT", pp, pc))
    cur.execute("""SELECT count(*), sum(clms) FROM exp5_combined
                   WHERE baseline_stage='9_residual' AND NOT set_matched""")
    n, c = cur.fetchone()
    emit("  still residual: %s pairs / %s claims" % (f"{n:,}", f"{c:,.0f}"))

    emit()
    emit("STILL RESIDUAL -- top 30")
    cur.execute("""SELECT e.brnd_name, e.gnrc_name, e.clms, c.parsed
                   FROM exp5_combined e
                   LEFT JOIN cms_set5 c USING (brnd_name, gnrc_name)
                   WHERE e.baseline_stage='9_residual' AND NOT e.set_matched
                   ORDER BY e.clms DESC NULLS LAST LIMIT 30""")
    emit("  %-30s %-28s %11s %-26s" % ("Brnd_Name", "Gnrc_Name", "claims", "parsed"))
    for b, g, c, p in cur.fetchall():
        emit("  %-30s %-28s %11s %-26s"
             % (str(b)[:30], str(g)[:28], f"{c:,.0f}" if c else "-", str(p)[:26]))
    con.close()
    emit()
    emit("written to " + OUT)


main()
