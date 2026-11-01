"""Completeness across the three source domains -- FDA, NUCC, NPI.

WHY THIS EXISTS. The port is mechanical (BR-028): nesting only, no
computation, no defaulting. That only works if we know in advance which
fields carry values and which are empty, because a port cannot invent a
field and must not default one. This is that inventory, MEASURED.

It answers the question we agreed was measurable rather than declared: which
source columns EXIST but are wholly empty in Delaware and Alabama. A field
absent for want of values is a different fact from a field absent for want of
existence, and the schema must be able to tell them apart.

THE RISK THIS BOUNDS. Two states cannot reveal a field that is populated only
elsewhere. Three things bound that: the CMS Part D file is a fixed 22-column
layout identical for every state; the FDA labels, ICD-10-CM, CCSR and coverage
articles are loaded NATIONALLY; and the provider collection's field set is our
own. The two states were also chosen as opposite ends of the data-quality
range, so a field empty in both is unlikely to matter anywhere.
"""
import sys
import os
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\completeness_result.txt"


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def column_coverage(cur, table, skip=()):
    """Per column: how many rows carry a value. Text emptiness counts as
    absent, because an empty string is not an answer."""
    cur.execute("""SELECT column_name, data_type
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s
                   ORDER BY ordinal_position""", (table,))
    cols = [(c, t) for c, t in cur.fetchall() if c not in skip]
    if not cols:
        return None, []
    cur.execute('SELECT count(*) FROM "%s"' % table)
    total = cur.fetchone()[0]
    if total == 0:
        return 0, []
    parts = []
    for c, t in cols:
        if t in ("text", "character varying", "character"):
            parts.append('count(*) FILTER (WHERE "%s" IS NOT NULL '
                         'AND btrim("%s") <> \'\')' % (c, c))
        else:
            parts.append('count("%s")' % c)
    cur.execute("SELECT %s FROM \"%s\"" % (", ".join(parts), table))
    counts = cur.fetchone()
    return total, [(c, t, n) for (c, t), n in zip(cols, counts)]


def report_table(cur, title, table, skip=()):
    total, rows = column_coverage(cur, table, skip)
    emit()
    emit("  %s -- %s (%s rows)" % (title, table,
                                   f"{total:,}" if total else "0"))
    if not rows:
        emit("     table absent or empty")
        return []
    empty = []
    emit("     %-42s %12s %8s" % ("column", "populated", "share"))
    for c, t, n in rows:
        pct = 100.0 * n / total if total else 0.0
        mark = ""
        if n == 0:
            mark = "   <-- EMPTY in these states"
            empty.append(c)
        elif pct < 50:
            mark = "   (sparse)"
        emit("     %-42s %12s %7.1f%%%s" % (c[:42], f"{n:,}", pct, mark))
    return empty


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("COMPLETENESS ACROSS THE THREE SOURCE DOMAINS -- FDA, NUCC, NPI")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("purpose: the port is nesting-only, so every field it emits must "
         "already exist as a column with values")
    emit()

    con = connect(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='3600s'")
    all_empty = {}

    # ================================================================== FDA
    emit("=" * 70)
    emit("1. FDA -- drug labels and the molecule identity built from them")
    emit("=" * 70)
    cur.execute("""SELECT count(*), count(DISTINCT set_id),
                          count(*) FILTER (WHERE has_indication),
                          count(DISTINCT set_id) FILTER (WHERE product_type
                                IS NOT NULL AND product_type <> '')
                   FROM fda_label""")
    n, sids, withind, withtype = cur.fetchone()
    emit()
    emit("  label rows loaded (national, OTC excluded) : %s" % f"{n:,}")
    emit("  distinct set_id                            : %s" % f"{sids:,}")
    emit("  carrying indication prose                  : %s (%.1f%%)"
         % (f"{withind:,}", 100.0 * withind / max(n, 1)))
    emit("  carrying an openfda product_type           : %s (%.1f%%)"
         % (f"{withtype:,}", 100.0 * withtype / max(sids, 1)))
    emit("  NOTE: the FDA record publishes 122 top-level fields. 12 were "
         "loaded; the cohort sections are now additionally kept (BR-020).")

    cur.execute("""SELECT count(*), count(DISTINCT molecule_id) FROM mol_label""")
    ml, mm = cur.fetchone()
    cur.execute("""SELECT count(DISTINCT molecule_id) FROM cms_drug_resolution
                   WHERE molecule_id IS NOT NULL""")
    emit("  molecule-to-label links                    : %s over %s molecules"
         % (f"{ml:,}", f"{mm:,}"))
    emit("  molecules reached from the claim file       : %s"
         % f"{cur.fetchone()[0]:,}")

    all_empty["fda_label"] = report_table(cur, "FDA label", "fda_label",
                                          skip=("load_seq", "source_blob"))

    cur.execute("""SELECT to_regclass('public.fda_label_cohort') IS NOT NULL""")
    if cur.fetchone()[0]:
        emit()
        emit("  COHORT NOTES (BR-020) -- stored verbatim, never interpreted")
        cur.execute("""SELECT count(*), count(DISTINCT set_id) FROM fda_label_cohort""")
        cn, cs = cur.fetchone()
        emit("     rows=%s  distinct set_id=%s" % (f"{cn:,}", f"{cs:,}"))
        if cn != cs:
            emit("     NOTE: rows exceed distinct set_id -- multiple cached "
                 "download generations. Dedupe to the latest effective_time "
                 "before any port.")
        all_empty["fda_label_cohort"] = report_table(
            cur, "FDA cohort notes", "fda_label_cohort")

    # ================================================================= NUCC
    emit()
    emit("=" * 70)
    emit("2. NUCC -- the specialty vocabulary that classifies providers")
    emit("=" * 70)
    cur.execute("SELECT count(*) FROM nucc_specialty")
    emit()
    emit("  vocabulary entries : %s" % f"{cur.fetchone()[0]:,}")
    emit("  NOTE: nucc_specialty holds the vocabulary as a JSONB blob. A "
         "nesting-only port (BR-028) cannot reach into JSON -- that is "
         "computation -- so the keys are surfaced as COLUMNS in chain_nucc. "
         "Surfacing an existing key is mechanical; nothing is computed.")
    all_empty["nucc_specialty"] = report_table(cur, "NUCC vocabulary",
                                               "nucc_specialty")
    all_empty["chain_nucc"] = report_table(cur, "NUCC as columns", "chain_nucc")
    emit()
    emit("  REACH -- is the vocabulary complete for the providers in scope?")
    cur.execute("""SELECT count(DISTINCT primary_nucc) FROM provider_nucc
                   WHERE primary_nucc IS NOT NULL AND primary_nucc <> ''""")
    used = cur.fetchone()[0]
    cur.execute("""SELECT count(DISTINCT pn.primary_nucc)
                   FROM provider_nucc pn
                   WHERE pn.primary_nucc IS NOT NULL AND pn.primary_nucc <> ''
                     AND NOT EXISTS (SELECT 1 FROM nucc_specialty ns
                                     WHERE ns.nucc_code = pn.primary_nucc)""")
    missing = cur.fetchone()[0]
    emit("     distinct primary NUCC codes held by providers : %s" % f"{used:,}")
    if missing is not None:
        emit("     of those, ABSENT from the vocabulary          : %s" % f"{missing:,}")
    cur.execute("""SELECT count(*) FILTER (WHERE primary_nucc IS NULL
                                             OR primary_nucc = ''),
                          count(*) FROM provider_nucc""")
    nonucc, totn = cur.fetchone()
    emit("     providers with NO primary NUCC                : %s of %s (%.2f%%)"
         % (f"{nonucc:,}", f"{totn:,}", 100.0 * nonucc / max(totn, 1)))

    # ================================================================== NPI
    emit()
    emit("=" * 70)
    emit("3. NPI -- the provider registry")
    emit("=" * 70)
    cur.execute("""SELECT count(*), count(DISTINCT npi) FROM provider""")
    pn, pd = cur.fetchone()
    emit()
    emit("  providers ported                : %s rows, %s distinct NPI"
         % (f"{pn:,}", f"{pd:,}"))
    if pn != pd:
        emit("     WARNING: provider is not 1:1 on NPI")
    cur.execute("""SELECT count(*), count(DISTINCT npi)
                   FROM provider_business_address""")
    bn, bd = cur.fetchone()
    emit("  BILLING addresses (BR-010)      : %s rows, %s NPI -- %s"
         % (f"{bn:,}", f"{bd:,}",
            "1:1, valid anchor" if bn == bd else "NOT 1:1, INVALID ANCHOR"))
    cur.execute("""SELECT count(*), count(DISTINCT npi),
                          max(c) FROM (SELECT npi, count(*) AS c
                                       FROM provider_practice_address
                                       GROUP BY 1) x""")
    an, ad, worst = cur.fetchone()
    emit("  PRACTICE addresses (BR-011)     : %s rows over %s NPI, worst %s "
         "on one NPI -- display only" % (f"{an:,}", f"{ad:,}", worst))
    cur.execute("SELECT count(*), count(DISTINCT npi) FROM provider_license")
    ln, ld = cur.fetchone()
    emit("  licenses                        : %s rows over %s NPI"
         % (f"{ln:,}", f"{ld:,}"))
    cur.execute("SELECT count(*), count(DISTINCT npi) FROM provider_taxonomy")
    tn, td = cur.fetchone()
    emit("  taxonomies                      : %s rows over %s NPI"
         % (f"{tn:,}", f"{td:,}"))

    emit()
    emit("  COVERAGE OF THE CLAIM FILE -- measured, not assumed")
    cur.execute("SELECT count(DISTINCT prscrbr_npi) FROM partd")
    cms = cur.fetchone()[0]
    cur.execute("""SELECT count(DISTINCT d.prscrbr_npi) FROM partd d
                   JOIN provider p ON p.npi = d.prscrbr_npi::text""")
    matched = cur.fetchone()[0]
    cur.execute("""SELECT count(DISTINCT d.prscrbr_npi) FROM partd d
                   JOIN provider_business_address b ON b.npi = d.prscrbr_npi::text""")
    anchored = cur.fetchone()[0]
    emit("     distinct prescriber NPI in the claim file  : %s" % f"{cms:,}")
    emit("     matched a provider record (on NPI alone)   : %s (%.2f%%)"
         % (f"{matched:,}", 100.0 * matched / max(cms, 1)))
    emit("     carrying a billing address                 : %s (%.2f%%)"
         % (f"{anchored:,}", 100.0 * anchored / max(cms, 1)))

    for t in ("provider", "provider_business_address",
              "provider_practice_address", "provider_nucc",
              "provider_license", "provider_taxonomy"):
        all_empty[t] = report_table(cur, "NPI", t, skip=("load_seq",))

    # =========================================== the empty-column report
    emit()
    emit("=" * 70)
    emit("4. FIELDS THAT EXIST BUT ARE EMPTY IN DELAWARE AND ALABAMA")
    emit("=" * 70)
    emit()
    emit("  These are the fields the port will omit for want of VALUES rather")
    emit("  than for want of EXISTENCE. The schema must be able to tell those")
    emit("  apart, and this is the list -- measured, not declared.")
    emit()
    any_empty = False
    for t, cols in sorted(all_empty.items()):
        if cols:
            any_empty = True
            emit("  %s:" % t)
            for c in cols:
                emit("     %s" % c)
    if not any_empty:
        emit("  NONE. Every column in every source table carries values in "
             "these two states.")

    # ============================================= the delivered collection
    emit()
    emit("=" * 70)
    emit("5. THE DELIVERED COLLECTION -- what the port will carry")
    emit("=" * 70)
    cur.execute("SELECT to_regclass('public.chain_delivered') IS NOT NULL")
    if not cur.fetchone()[0]:
        emit("  chain_delivered absent -- run build_chain.py first")
    else:
        cur.execute("""SELECT count(*), count(DISTINCT npi),
                              count(DISTINCT molecule_id),
                              count(DISTINCT indication_id),
                              count(DISTINCT icd10_code)
                       FROM chain_delivered""")
        n, p, m, i, c = cur.fetchone()
        emit()
        emit("  delivered rows   : %s" % f"{n:,}")
        emit("  providers        : %s" % f"{p:,}")
        emit("  molecules        : %s" % f"{m:,}")
        emit("  indications      : %s" % f"{i:,}")
        emit("  ICD codes        : %s" % f"{c:,}")
        cur.execute("""SELECT count(*) FROM (SELECT DISTINCT npi, molecule_id
                                             FROM chain_delivered) x""")
        emit("  ProviderMedicare documents at the approved grain "
             "(one per molecule per provider, BR-024): %s"
             % f"{cur.fetchone()[0]:,}")
        all_empty["chain_delivered"] = report_table(
            cur, "delivered", "chain_delivered")

    con.close()
    emit()
    emit("written to " + OUT)


main()
