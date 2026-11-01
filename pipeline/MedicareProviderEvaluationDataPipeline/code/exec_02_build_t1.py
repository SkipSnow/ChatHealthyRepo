"""Execution step 2 -- build T1, cms_drug_resolution, over the DE+AL pairs.

Grain: one row per (Brnd_Name, Gnrc_Name) occurring in Delaware or Alabama.
1,910 expected. Every row carries the stage that resolved it and the
provenance of that stage, so inference is separable from a join.

TWO DESIGN POINTS implementing operator rulings:

1. FOLD FOR MATCHING, PRESERVE PRECISION FOR IDENTITY.
   The has_form salt fold is what lets CMS "hydrocodone" meet FDA
   "HYDROCODONE BITARTRATE". But folding also collapses pegylated liposomal
   doxorubicin onto plain doxorubicin, and the operator has ruled that
   clinically distinct formulations must NOT roll up together. So the fold is
   used to compute the MATCH key and the UNFOLDED, most-precise concept set is
   used as the molecule identity. Two products can therefore match the same
   labels while keeping different molecule_ids.

2. S4 IS STAMPED AND SEGREGATED.
   Unique prefix extension is the only inferred stage in T1. Every S4 row is
   stamped provenance='inference' and coverage is reported with S4 included
   and excluded, so the cost of excluding it is visible rather than argued.

Trial-design artifacts are removed from the trials source on BOTH sides of
every alias edge, per the operator's rule, not merely blocked as a target.
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

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (  # noqa: E402
    brand_join_key)

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline\code\exec_02_t1_result.txt"
MIN_PREFIX = 6

SPLIT = [" with ", " and ", " w/", "/", "-", "+"]
NOISE = re.compile(
    r"\b(\d+(\.\d+)?\s*(mg|mcg|g|ml|l|unit|units|iu|meq|mmol|%|hr|hour)s?"
    r"|er|xr|sr|cr|dr|la|xl|ec|odt|hfa|pf|inj|iv|im|po|sl"
    r"|tablet|tablets|capsule|capsules|cap|caps|tab|tabs|solution|soln"
    r"|suspension|susp|cream|ointment|gel|patch|spray|drops|syrup|elixir"
    r"|kit|pack|pen|vial|ampul|ampule|syringe|inhaler|aerosol|powder"
    r"|oral|topical|ophthalmic|otic|nasal|rectal|vaginal|subcutaneous"
    r"|extended|release|delayed|immediate|chewable|coated|film)\b", re.I)

ARTIFACTS = {"PLACEBO", "PLACEBOS", "MATCHING PLACEBO", "SALINE", "NORMAL SALINE",
             "SALINE SOLUTION", "STERILE WATER", "WATER", "VEHICLE", "SHAM",
             "SUGAR PILL", "NO INTERVENTION", "CONTROL", "STANDARD OF CARE",
             "USUAL CARE", "DILUENT", "EXCIPIENT", "ADJUVANT", "NONE", "N/A",
             "NA", "NO OTHER NAMES"}

NON_DRUG = ("pen needle", "needle", "syringe", "lancet", "test strip",
            "alcohol prep", "alcohol antiseptic", "swab", "gauze", "tubing",
            "insulin pump", "infusion set", "reservoir", "blood glucose",
            "monitor", "meter", "spacer", "nebulizer", "catheter", "dressing",
            "bandage", "wipes", "applicator", "diabetic supply")
VACCINE = ("vacc", "vaccine", "toxoid", "antigen", "tdap", "dtap", "mmr",
           "hepatitis a", "hepatitis b", "influenza", "zoster", "pneumococcal",
           "rsvpref", "rsv ", "meningococcal", "papillomavirus", "rabies",
           "typhoid", "polio", "varicella", "immune globulin")


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def clean(t):
    t = NOISE.sub(" ", (t or "").strip().lower())
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tokens(name):
    s = " " + (name or "").lower() + " "
    parts = [s]
    for sep in SPLIT:
        nxt = []
        for p in parts:
            nxt.extend(p.split(sep))
        parts = nxt
    out, seen = [], set()
    for p in parts:
        c = clean(p)
        if len(c) >= 3 and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def load_vocab(cur):
    """Two lookups: precise (identity) and folded (matching)."""
    cur.execute("""SELECT upper(c.str_) AS nm, c.rxcui, c.tty
                   FROM rxnconso c
                   WHERE c.lat='ENG' AND c.suppress='N'
                     AND c.tty IN ('IN','PIN','MIN')
                     AND c.str_ IS NOT NULL AND c.str_ <> ''""")
    precise = {}
    for nm, cui, tty in cur.fetchall():
        precise.setdefault(clean(nm), cui)
    cur.execute("""SELECT DISTINCT r.rxcui1, r.rxcui2 FROM rxnrel r
                   JOIN (SELECT DISTINCT rxcui FROM rxnconso
                         WHERE tty='IN' AND lat='ENG' AND suppress='N') i
                     ON i.rxcui = r.rxcui2
                   WHERE r.rela='has_form' AND r.rxcui1 <> r.rxcui2""")
    fold = {a: b for a, b in cur.fetchall()}
    single = sorted((n, c) for n, c in precise.items()
                    if not any(s in n for s in (" and ", "/", "+", " with ")))
    emit("vocabulary: precise names=%s  has_form folds=%s  single-ingredient "
         "prefix list=%s" % (f"{len(precise):,}", f"{len(fold):,}",
                             f"{len(single):,}"))
    return precise, fold, single


def make_extend(single):
    import bisect
    keys = [n for n, _ in single]

    def extend(tok):
        if len(tok) < MIN_PREFIX:
            return None, 0
        i = bisect.bisect_left(keys, tok)
        cands, j = set(), i
        while j < len(keys) and keys[j].startswith(tok):
            cands.add(single[j][1]); j += 1
            if len(cands) > 1 and j - i > 200:
                break
        return (next(iter(cands)), 1) if len(cands) == 1 else (None, len(cands))
    return extend


def resolve(toks, precise, fold, extend):
    """-> (identity_set, match_set, used_inference)"""
    ident, match, inferred = [], [], False
    for t in toks:
        cui = precise.get(t)
        if cui is None:
            cui, _ = extend(t)
            if cui is not None:
                inferred = True
        if cui is None:
            ident.append("lit:" + t); match.append("lit:" + t)
        else:
            ident.append("rx:" + str(cui))                      # precise
            match.append("rx:" + str(fold.get(cui, cui)))       # folded
    return (("|".join(sorted(set(ident)))),
            ("|".join(sorted(set(match)))), inferred)


def classify_residual(b, g):
    both = (b + " " + g).lower()
    if any(m in both for m in NON_DRUG):
        return "not_a_drug"
    if any(m in both for m in VACCINE):
        return "vaccine_biologic"
    if len(b) >= 29 or len(g) >= 29:
        return "cms_truncated"
    return "unknown_to_vocabularies"


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("EXECUTION STEP 2 -- T1 cms_drug_resolution over Delaware + Alabama")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()
    con = connect(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='1800s'")

    precise, fold, single = load_vocab(cur)
    extend = make_extend(single)

    # ---- FDA side: identity and match sets per label ----------------------
    cur.execute("""SELECT set_id, array_agg(DISTINCT value) FROM fda_name
                   WHERE kind='substance' AND value <> '' GROUP BY set_id""")
    buf = io.StringIO(); w = csv.writer(buf)
    for sid, vals in cur.fetchall():
        toks, seen = [], set()
        for v in vals:
            for t in tokens(v):
                if t not in seen:
                    seen.add(t); toks.append(t)
        ident, match, _ = resolve(toks, precise, fold, extend)
        w.writerow([sid, ident, match, " + ".join(sorted(toks))])
    buf.seek(0)
    cur.execute("DROP TABLE IF EXISTS fda_mol")
    cur.execute("""CREATE TABLE fda_mol (set_id text, ident_set text,
                                         match_set text, parsed text)""")
    cur.copy_expert("COPY fda_mol FROM STDIN WITH (FORMAT csv)", buf)
    cur.execute("CREATE INDEX fm_match ON fda_mol (match_set)")
    cur.execute("CREATE INDEX fm_ident ON fda_mol (ident_set)")
    cur.execute("ANALYZE fda_mol")
    cur.execute("SELECT count(*), count(DISTINCT match_set), count(DISTINCT ident_set) FROM fda_mol")
    a, b_, c_ = cur.fetchone()
    emit("fda_mol: labels=%s distinct match sets=%s distinct identity sets=%s"
         % (f"{a:,}", f"{b_:,}", f"{c_:,}"))
    emit("  (identity sets exceed match sets: that difference IS the "
         "formulation distinction the fold would have erased)")

    # ---- brand lookups -----------------------------------------------------
    cur.execute("DROP TABLE IF EXISTS fda_brand")
    cur.execute("""CREATE TABLE fda_brand AS
                   SELECT DISTINCT upper(value) AS nm, set_id FROM fda_name
                   WHERE kind='brand' AND value <> ''""")
    cur.execute("CREATE INDEX fb_nm ON fda_brand (nm)")
    cur.execute("DROP TABLE IF EXISTS fda_brand_norm")
    cur.execute("""CREATE TABLE fda_brand_norm AS
                   SELECT DISTINCT value_norm AS nm, set_id FROM fda_name
                   WHERE kind='brand' AND value_norm <> ''""")
    cur.execute("CREATE INDEX fbn_nm ON fda_brand_norm (nm)")
    cur.execute("ANALYZE fda_brand"); cur.execute("ANALYZE fda_brand_norm")

    # ---- trials bridge, artifacts removed BOTH sides ----------------------
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
        SELECT src, min(ing_rxcui) AS ing_rxcui, count(DISTINCT ing_rxcui) AS n_ing
        FROM edge_both GROUP BY src""", {"a": list(ARTIFACTS)})
    cur.execute("CREATE INDEX cbt_src ON ct_bridge_t1 (src)")
    cur.execute("ANALYZE ct_bridge_t1")
    cur.execute("SELECT count(*) FILTER (WHERE n_ing=1), count(*) FILTER (WHERE n_ing>1) FROM ct_bridge_t1")
    u, m = cur.fetchone()
    emit("trials bridge (artifact-free, both sides): unique=%s ambiguous=%s"
         % (f"{u:,}", f"{m:,}"))
    emit()

    # ---- the scoped CMS pairs ---------------------------------------------
    cur.execute("""SELECT brnd_name, gnrc_name,
                          sum(src_rows), sum(tot_clms), sum(tot_drug_cst)
                   FROM partd_pair_state GROUP BY 1,2""")
    pairs = cur.fetchall()
    emit("DE+AL distinct pairs to resolve: %s" % f"{len(pairs):,}")

    rows = []
    for b, g, srows, clms, cost in pairs:
        b = (b or "").strip(); g = (g or "").strip()
        stage = provenance = None
        molecule = match_set = ident_set = ""
        inferred = False

        bu = b.upper()
        cur.execute("SELECT count(*) FROM fda_brand WHERE nm=%s", (bu,))
        if cur.fetchone()[0]:
            stage, provenance = "S1_brand_exact", "joined_coded_source"
        if stage is None:
            bn = brand_join_key(b) or ""
            if bn:
                cur.execute("SELECT count(*) FROM fda_brand_norm WHERE nm=%s", (bn,))
                if cur.fetchone()[0]:
                    stage, provenance = "S2_brand_normalized", "adopted_vocabulary"

        toks = tokens(g) or tokens(b)
        ident_set, match_set, inferred = resolve(toks, precise, fold, extend)
        if stage is None and match_set:
            cur.execute("SELECT count(*) FROM fda_mol WHERE match_set=%s", (match_set,))
            if cur.fetchone()[0]:
                if inferred:
                    stage, provenance = "S4_prefix_extended", "inference"
                else:
                    stage, provenance = "S3_ingredient_set", "joined_coded_source"
        if stage is None:
            for probe in (bu, g.upper()):
                cur.execute("SELECT ing_rxcui FROM ct_bridge_t1 "
                            "WHERE src=%s AND n_ing=1", (probe,))
                r = cur.fetchone()
                if r:
                    stage, provenance = "S5_trials_bridge", "joined_coded_source"
                    ident_set = ident_set or ("rx:" + str(r[0]))
                    break

        if stage is None:
            reason = classify_residual(b, g)
            rows.append([b, g, "", "9_residual", "unresolved", ident_set,
                         match_set, 0, reason, srows, clms or 0, cost or 0,
                         "false"])
        else:
            molecule = ident_set     # identity is the PRECISE set, never folded
            cur.execute("SELECT count(DISTINCT set_id) FROM fda_mol WHERE match_set=%s",
                        (match_set,))
            nl = cur.fetchone()[0] if match_set else 0
            rows.append([b, g, molecule, stage, provenance, ident_set,
                         match_set, nl, "", srows, clms or 0, cost or 0,
                         "true" if stage == "S4_prefix_extended" else "false"])

    buf = io.StringIO(); w = csv.writer(buf)
    for r in rows:
        w.writerow(r)
    buf.seek(0)
    cur.execute("DROP TABLE IF EXISTS cms_drug_resolution")
    cur.execute("""CREATE TABLE cms_drug_resolution (
                     brnd_name text, gnrc_name text, molecule_id text,
                     resolution_stage text, provenance text,
                     ingredient_set text, match_set text, n_labels int,
                     residual_reason text, src_rows bigint,
                     tot_clms double precision, tot_drug_cst double precision,
                     is_inference boolean)""")
    cur.copy_expert("COPY cms_drug_resolution FROM STDIN WITH (FORMAT csv)", buf)
    cur.execute("CREATE INDEX t1_k ON cms_drug_resolution (brnd_name, gnrc_name)")
    cur.execute("CREATE INDEX t1_mol ON cms_drug_resolution (molecule_id)")
    cur.execute("ANALYZE cms_drug_resolution")
    emit("T1 built: %s rows" % f"{len(rows):,}")
    emit()

    # ---- reporting ---------------------------------------------------------
    emit("=== STAGE DISTRIBUTION (both states, pair-grained) ===")
    cur.execute("""SELECT resolution_stage, provenance, count(*),
                          sum(tot_clms),
                          round((100.0*count(*)/sum(count(*)) OVER ())::numeric,2),
                          round((100.0*sum(tot_clms)/sum(sum(tot_clms)) OVER ())::numeric,2)
                   FROM cms_drug_resolution GROUP BY 1,2 ORDER BY 1""")
    emit("  %-22s %-20s %7s %14s %8s %9s"
         % ("stage", "provenance", "pairs", "claims", "pairs%", "claims%"))
    for s, p, n, c, pp, pc in cur.fetchall():
        emit("  %-22s %-20s %7s %14s %7.2f%% %8.2f%%"
             % (s, p, f"{n:,}", f"{c:,.0f}", pp, pc))

    emit()
    emit("=== RESOLUTION, WITH AND WITHOUT THE INFERRED STAGE ===")
    cur.execute("""SELECT
      round((100.0*count(*) FILTER (WHERE resolution_stage<>'9_residual')/count(*))::numeric,2),
      round((100.0*sum(CASE WHEN resolution_stage<>'9_residual' THEN tot_clms ELSE 0 END)
            /sum(tot_clms))::numeric,2),
      round((100.0*count(*) FILTER (WHERE resolution_stage NOT IN ('9_residual','S4_prefix_extended'))
            /count(*))::numeric,2),
      round((100.0*sum(CASE WHEN resolution_stage NOT IN ('9_residual','S4_prefix_extended')
                            THEN tot_clms ELSE 0 END)/sum(tot_clms))::numeric,2)
      FROM cms_drug_resolution""")
    a, b_, c_, d_ = cur.fetchone()
    emit("  including S4 inference : pairs=%.2f%%  claims=%.2f%%" % (a, b_))
    emit("  EXCLUDING S4 inference : pairs=%.2f%%  claims=%.2f%%" % (c_, d_))
    emit("  cost of excluding it   : pairs=%.2f pts  claims=%.2f pts"
         % (a - c_, b_ - d_))

    emit()
    emit("=== DELAWARE vs ALABAMA, never averaged ===")
    cur.execute("""
      SELECT s.state, count(*), sum(s.tot_clms),
        round((100.0*count(*) FILTER (WHERE t.resolution_stage<>'9_residual')
              /count(*))::numeric,2),
        round((100.0*sum(CASE WHEN t.resolution_stage<>'9_residual'
                              THEN s.tot_clms ELSE 0 END)/sum(s.tot_clms))::numeric,2)
      FROM partd_pair_state s
      JOIN cms_drug_resolution t USING (brnd_name, gnrc_name)
      GROUP BY 1 ORDER BY 1""")
    emit("  %-6s %7s %15s %12s %12s"
         % ("state", "pairs", "claims", "pairs%", "claims%"))
    for st, n, c, pp, pc in cur.fetchall():
        emit("  %-6s %7s %15s %11.2f%% %11.2f%%" % (st, f"{n:,}", f"{c:,.0f}", pp, pc))

    emit()
    emit("=== RESIDUAL BY REASON ===")
    cur.execute("""SELECT residual_reason, count(*), sum(tot_clms)
                   FROM cms_drug_resolution WHERE resolution_stage='9_residual'
                   GROUP BY 1 ORDER BY 3 DESC""")
    for r, n, c in cur.fetchall():
        emit("  %-26s pairs=%-5s claims=%s" % (r, f"{n:,}", f"{c:,.0f}"))

    emit()
    emit("=== FORMULATION DISTINCTION PRESERVED (operator ruling) ===")
    cur.execute("""SELECT count(DISTINCT molecule_id), count(DISTINCT match_set)
                   FROM cms_drug_resolution WHERE molecule_id <> ''""")
    mi, ms = cur.fetchone()
    emit("  distinct molecule_id (precise)=%s   distinct match_set (folded)=%s"
         % (f"{mi:,}", f"{ms:,}"))
    emit("  %s pairs would have collapsed had identity used the folded set"
         % f"{mi - ms:,}")
    cur.execute("""SELECT brnd_name, gnrc_name, molecule_id, match_set
                   FROM cms_drug_resolution
                   WHERE molecule_id <> '' AND molecule_id <> match_set
                   ORDER BY tot_clms DESC LIMIT 8""")
    for b, g, mol, mt in cur.fetchall():
        emit("    %-28s %-26s identity=%-18s folded=%s"
             % (str(b)[:28], str(g)[:26], mol[:18], mt[:18]))
    con.close()
    emit()
    emit("written to " + OUT)


# Guarded so the vocabulary and resolution functions can be imported by the
# FDA-only molecule build without running the CMS bridge, which this file also
# contains. The FDA collection needs load_vocab/make_extend/tokens/resolve and
# nothing CMS-shaped.
if __name__ == "__main__":
    main()
