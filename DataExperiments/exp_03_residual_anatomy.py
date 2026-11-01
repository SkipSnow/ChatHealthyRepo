"""Experiment 3 -- what the 559 unresolved pairs actually ARE.

Before writing a matcher, characterise the failure. Each residual pair is
classified by inspectable evidence, not by guesswork, and every category
carries its pair count and its claim volume so effort can go where the
volume is.

Also audits the PRECISION of what the baseline already "resolved": a pair
matching many labels is only acceptable if those labels agree on the
ingredient. 375 brand-exact pairs matched more than ten labels and one
matched 386, and a resolution rate that counts those as successes is lying.

Nothing here writes to the funnel; it only reads and reports.
"""
import os
import sys
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC")
sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\exp_03_result.txt"

# CMS separates the ingredients of a combination product with these.
COMBO_SEPARATORS = ("-", "/", ",", " And ", " W/", " With ")

# Part D pays for these, and they are not drugs: no molecule, no FDA label.
NON_DRUG_MARKERS = (
    "pen needle", "needle", "syringe", "lancet", "test strip", "glucose test",
    "alcohol prep", "alcohol antiseptic", "swab", "gauze", "tubing",
    "insulin pump", "infusion set", "reservoir", "diabetic supply",
    "blood glucose", "monitor", "meter", "spacer", "nebulizer", "inhaler device",
    "catheter", "dressing", "bandage", "wipes", "applicator",
)

# Biologics licensed under a BLA. These are frequently not in drug/label at all.
VACCINE_MARKERS = (
    "vacc", "vaccine", "toxoid", "antigen", "tdap", "dtap", "mmr",
    "hepatitis a", "hepatitis b", "influenza", "zoster", "pneumococcal",
    "rsvpref", "rsv ", "meningococcal", "papillomavirus", "rabies",
    "typhoid", "yellow fever", "polio", "varicella", "immune globulin",
)


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def has_any(text, markers):
    t = text.lower()
    return any(m.lower() in t for m in markers)


def combo_parts(brand, generic):
    """How many ingredients CMS appears to be naming."""
    best = 1
    for s in COMBO_SEPARATORS:
        for field in (brand, generic):
            n = len([p for p in field.split(s) if p.strip()])
            best = max(best, n)
    return best


def classify(brand, generic):
    both = brand + " " + generic
    if has_any(both, NON_DRUG_MARKERS):
        return "A_not_a_drug"
    if has_any(both, VACCINE_MARKERS):
        return "B_vaccine_or_biologic"
    if combo_parts(brand, generic) > 1:
        return "C_combination_product"
    if len(brand) >= 29 or len(generic) >= 29:
        return "D_cms_name_truncated"
    if any(ch in generic for ch in ".,"):
        return "E_cms_abbreviated"
    return "F_unexplained"


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("EXPERIMENT 3 -- anatomy of the residual, and precision of the resolved")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()

    con = connect()
    cur = con.cursor()

    # ---------- part 1: what is unresolved -------------------------------
    cur.execute("""SELECT brnd_name, gnrc_name, clms, in_dev_set
                   FROM funnel_pair WHERE stage='9_residual'""")
    rows = cur.fetchall()
    buckets = {}
    for b, g, c, dev in rows:
        k = classify(b or "", g or "")
        acc = buckets.setdefault(k, {"n": 0, "clms": 0.0, "ex": []})
        acc["n"] += 1
        acc["clms"] += (c or 0)
        if len(acc["ex"]) < 6:
            acc["ex"].append((b, g, c))

    cur.execute("SELECT count(*), sum(clms) FROM funnel_pair")
    all_pairs, all_clms = cur.fetchone()
    res_pairs = len(rows)
    res_clms = sum((c or 0) for _, _, c, _ in rows)

    emit("RESIDUAL BY CATEGORY  (%s pairs, %s claims = %.2f%% of all claims)"
         % (f"{res_pairs:,}", f"{res_clms:,.0f}", 100.0 * res_clms / all_clms))
    emit()
    emit("  %-24s %7s %15s %9s %9s"
         % ("category", "pairs", "claims", "% of res", "% all clm"))
    for k in sorted(buckets):
        a = buckets[k]
        emit("  %-24s %7s %15s %8.1f%% %8.2f%%"
             % (k, f"{a['n']:,}", f"{a['clms']:,.0f}",
                100.0 * a["n"] / res_pairs, 100.0 * a["clms"] / all_clms))
    emit()
    for k in sorted(buckets):
        emit("  %s -- examples" % k)
        for b, g, c in buckets[k]["ex"]:
            emit("     %-34s %-34s %12s"
                 % (str(b)[:34], str(g)[:34], f"{c:,.0f}" if c else "-"))
        emit()

    # What is recoverable in principle, and what is not a drug at all.
    not_drug = buckets.get("A_not_a_drug", {"n": 0, "clms": 0.0})
    vaccine = buckets.get("B_vaccine_or_biologic", {"n": 0, "clms": 0.0})
    emit("DENOMINATOR -- a device is not a failed drug match")
    emit("  all pairs                      %7s" % f"{all_pairs:,}")
    emit("  not a drug (devices, supplies) %7s  %s claims"
         % (f"{not_drug['n']:,}", f"{not_drug['clms']:,.0f}"))
    emit("  vaccine / biologic             %7s  %s claims"
         % (f"{vaccine['n']:,}", f"{vaccine['clms']:,.0f}"))
    drug_pairs = all_pairs - not_drug["n"]
    drug_clms = all_clms - not_drug["clms"]
    resolved_pairs = all_pairs - res_pairs
    emit("  drug pairs (fair denominator)  %7s" % f"{drug_pairs:,}")
    emit("  resolution against it          %7.2f%% of pairs, %.2f%% of claims"
         % (100.0 * resolved_pairs / drug_pairs,
            100.0 * (all_clms - res_clms) / drug_clms))
    emit()

    # ---------- part 2: precision of what was resolved -------------------
    emit("PRECISION AUDIT -- do the many labels a pair matched AGREE?")
    emit("A pair matching 386 labels is only a success if those labels name")
    emit("the same ingredient. Checking agreement via the labels' own")
    emit("substance names.")
    emit()
    cur.execute("""
        DROP TABLE IF EXISTS exp3_agree;
        CREATE TABLE exp3_agree AS
        SELECT h.stage, h.brnd_name, h.gnrc_name,
               count(DISTINCT h.set_id)   AS labels,
               count(DISTINCT n.value_norm) AS distinct_ingredients
        FROM funnel_hit h
        JOIN fda_name n ON n.set_id = h.set_id
                       AND n.kind IN ('substance','generic')
                       AND n.value_norm <> ''
        GROUP BY 1,2,3""")
    con.commit()
    cur.execute("""SELECT stage,
                      count(*),
                      count(*) FILTER (WHERE distinct_ingredients = 1),
                      count(*) FILTER (WHERE distinct_ingredients BETWEEN 2 AND 3),
                      count(*) FILTER (WHERE distinct_ingredients > 3),
                      max(distinct_ingredients)
                   FROM exp3_agree GROUP BY stage ORDER BY stage""")
    emit("  %-24s %7s %9s %9s %9s %6s"
         % ("stage", "pairs", "1 ingred", "2-3", ">3", "max"))
    for s, n, a, b, c, m in cur.fetchall():
        emit("  %-24s %7s %9s %9s %9s %6s"
             % (s, f"{n:,}", f"{a:,}", f"{b:,}", f"{c:,}", f"{m:,}"))
    emit()
    emit("  WORST OFFENDERS -- resolved pairs whose labels disagree most")
    cur.execute("""SELECT a.stage, a.brnd_name, a.gnrc_name, a.labels,
                          a.distinct_ingredients, p.clms
                   FROM exp3_agree a
                   JOIN funnel_pair p USING (brnd_name, gnrc_name)
                   ORDER BY a.distinct_ingredients DESC LIMIT 20""")
    emit("  %-20s %-28s %-26s %7s %7s %12s"
         % ("stage", "Brnd_Name", "Gnrc_Name", "labels", "ingred", "claims"))
    for s, b, g, nl, ni, c in cur.fetchall():
        emit("  %-20s %-28s %-26s %7s %7s %12s"
             % (s[:20], str(b)[:28], str(g)[:26], nl, ni,
                f"{c:,.0f}" if c else "-"))
    emit()
    cur.execute("""SELECT round(100.0*count(*) FILTER (WHERE distinct_ingredients=1)
                               /count(*),2) FROM exp3_agree""")
    emit("  pairs whose matched labels agree on ONE ingredient: %.2f%%"
         % cur.fetchone()[0])
    con.close()
    emit()
    emit("written to " + OUT)


main()
