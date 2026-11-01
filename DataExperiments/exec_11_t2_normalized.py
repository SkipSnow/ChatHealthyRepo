"""Execution step 11 -- rebuild T2 with the provider record properly joined.

Four corrections, each an operator ruling proved on this data:

1. THE BILLING ADDRESS IS THE ANCHOR. provider_business_address is 1:1 with NPI
   (137,173 rows for 137,173 NPIs). provider_practice_address is 1:many -- up to
   356 addresses for NPI 1578891008, whose single billing address is Blackwood,
   NJ. Joining the file through practice address inflates it 1.32x overall and
   356x for that provider. The file is therefore constructed on the canonical
   NPI -> billing address map; practice addresses are carried separately for
   display to the end user, where 1:many is correct.

2. TWO STATE FACTS, NEITHER SUBSTITUTED.
     fill_state     Part D Prscrbr_State_Abrvtn -- where the prescription was
                    FILLED. This is where the patients are, and it is the state
                    the chain reports on.
     billing_state  the firm's own record -- where the provider's billing office
                    is. 159 Alabama and 60 Delaware providers differ from their
                    fill state, and that is two correct facts, not an error.

3. NUCC IS THE SPECIALTY. provider_taxonomy.code, not Prscrbr_Type. The claim
   text disagrees with the primary NUCC label constantly -- 5,002 providers
   whose claim text says "Nurse Practitioner" carry primary NUCC
   "Women's Health Nurse Practitioner", and 1,790 whose text says
   "Family Practice" carry "Student in an Organized Health Care Education
   Program". The claim text is not a taxonomy and cannot be used as one.

4. NO INFERENCE. Joins and sums only.
"""
import sys
import os
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\exec_11_t2_result.txt"


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
    emit("EXECUTION STEP 11 -- T2 rebuilt on the canonical billing anchor")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()
    con = connect(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='3600s'")

    # ---- the canonical provider spine: exactly one row per NPI -------------
    cur.execute("""
        DROP TABLE IF EXISTS provider_spine;
        CREATE TABLE provider_spine AS
        SELECT p.npi,
               p.entity_type_code_label            AS entity_type,
               p.provider_last_name_legal_name     AS last_or_org_name,
               p.provider_first_name               AS first_name,
               p.provider_credential_text          AS credential,
               p.can_prescribe, p.is_homeopathic, p.is_primary_care,
               p.is_active, p.urban                AS provider_urban,
               p.filled_in_test_state, p.office_in_test_state,
               b.state  AS billing_state,
               b.city   AS billing_city,
               b.zip    AS billing_zip,
               b.county_fips AS billing_county_fips
        FROM provider p
        LEFT JOIN provider_business_address b ON b.npi = p.npi;
        CREATE UNIQUE INDEX ps_npi ON provider_spine (npi);
        CREATE INDEX ps_bstate ON provider_spine (billing_state);
    """)
    cur.execute("""SELECT count(*), count(DISTINCT npi) FROM provider_spine""")
    a, b = cur.fetchone()
    emit("provider_spine rows=%s distinct npi=%s  %s"
         % (f"{a:,}", f"{b:,}", "1:1 CONFIRMED" if a == b else "NOT 1:1 -- STOP"))
    if a != b:
        raise ChatHealthyException("runtime_error", "the spine is not 1:1; the anchor is invalid")

    # ---- NUCC specialty, aggregated to the NPI so it cannot fan out -------
    cur.execute("""
        DROP TABLE IF EXISTS provider_nucc;
        CREATE TABLE provider_nucc AS
        SELECT npi,
               array_agg(DISTINCT code ORDER BY code)        AS nucc_codes,
               (array_agg(code ORDER BY is_primary DESC, code))[1] AS primary_nucc,
               (array_agg(code_label ORDER BY is_primary DESC, code))[1] AS primary_nucc_label,
               count(*) AS n_taxonomies
        FROM provider_taxonomy WHERE code <> '' GROUP BY npi;
        CREATE UNIQUE INDEX pn_npi ON provider_nucc (npi);
    """)
    cur.execute("SELECT count(*), count(DISTINCT npi) FROM provider_nucc")
    a, b = cur.fetchone()
    emit("provider_nucc  rows=%s distinct npi=%s  %s"
         % (f"{a:,}", f"{b:,}", "1:1" if a == b else "NOT 1:1"))

    # ---- practice addresses aggregated, for DISPLAY only ------------------
    cur.execute("""
        DROP TABLE IF EXISTS provider_practice_display;
        CREATE TABLE provider_practice_display AS
        SELECT npi,
               count(*)                                   AS n_practice_addresses,
               array_agg(DISTINCT state ORDER BY state)    AS practice_states,
               bool_or(urban)                              AS any_practice_urban,
               min(rucc)                                   AS most_urban_rucc
        FROM provider_practice_address WHERE state <> '' GROUP BY npi;
        CREATE UNIQUE INDEX ppd_npi ON provider_practice_display (npi);
    """)
    cur.execute("SELECT count(*), max(n_practice_addresses) FROM provider_practice_display")
    a, mx = cur.fetchone()
    emit("provider_practice_display rows=%s  worst multiplicity=%s "
         "(carried AGGREGATED so it cannot fan the file out)" % (f"{a:,}", mx))

    # ---- T2 rebuilt -------------------------------------------------------
    cur.execute("DROP TABLE IF EXISTS provider_molecule_v2")
    cur.execute("""
        CREATE TABLE provider_molecule_v2 AS
        SELECT
          p.prscrbr_state_abrvtn                        AS fill_state,
          s.billing_state                               AS billing_state,
          (p.prscrbr_state_abrvtn IS DISTINCT FROM s.billing_state)
                                                        AS state_differs,
          p.prscrbr_npi                                 AS npi,
          s.entity_type, s.last_or_org_name, s.first_name, s.credential,
          s.can_prescribe, s.is_primary_care, s.is_active,
          n.primary_nucc, n.primary_nucc_label, n.nucc_codes,
          d.n_practice_addresses, d.practice_states, d.most_urban_rucc,
          -- the claim text, carried but NEVER the specialty
          max(p.prscrbr_type)                           AS claim_specialty_text,
          t.molecule_id,
          count(DISTINCT (p.brnd_name, p.gnrc_name))    AS n_source_pairs,
          count(*)                                      AS src_rows,
          sum(NULLIF(p.tot_clms,'')::double precision)      AS tot_clms,
          sum(NULLIF(p.tot_day_suply,'')::double precision) AS tot_day_suply,
          sum(NULLIF(p.tot_drug_cst,'')::double precision)  AS tot_drug_cst,
          sum(NULLIF(p.tot_benes,'')::double precision)     AS tot_benes,
          count(*) FILTER (WHERE NULLIF(p.tot_benes,'') IS NULL)
                                                        AS rows_benes_suppressed
        FROM partd p
        JOIN cms_drug_resolution t
          ON t.brnd_name = p.brnd_name AND t.gnrc_name = p.gnrc_name
         AND t.molecule_id <> ''
        -- the ANCHOR: 1:1, so the file cannot inflate
        LEFT JOIN provider_spine s ON s.npi = p.prscrbr_npi
        LEFT JOIN provider_nucc n ON n.npi = p.prscrbr_npi
        LEFT JOIN provider_practice_display d ON d.npi = p.prscrbr_npi
        GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,19
    """)
    for ddl in (
        "CREATE INDEX t2v2_fill ON provider_molecule_v2 (fill_state)",
        "CREATE INDEX t2v2_npi ON provider_molecule_v2 (npi)",
        "CREATE INDEX t2v2_mol ON provider_molecule_v2 (molecule_id)",
        "CREATE INDEX t2v2_nucc ON provider_molecule_v2 (primary_nucc)",
    ):
        cur.execute(ddl)
    cur.execute("ANALYZE provider_molecule_v2")

    emit()
    emit("=== T2 v2 -- no inflation against the NPI-keyed original ===")
    cur.execute("SELECT count(*) FROM provider_molecule")
    old = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM provider_molecule_v2")
    new = cur.fetchone()[0]
    emit("  original (no provider join) %s rows" % f"{old:,}")
    emit("  v2 (billing anchor joined)  %s rows   ratio %.4f"
         % (f"{new:,}", new / max(old, 1)))

    emit()
    emit("=== BY FILL STATE ===")
    cur.execute("""SELECT fill_state, count(*), count(DISTINCT npi),
                          count(DISTINCT molecule_id), sum(tot_clms)
                   FROM provider_molecule_v2 GROUP BY 1 ORDER BY 1""")
    emit("  %-5s %11s %11s %11s %15s"
         % ("state", "rows", "providers", "molecules", "claims"))
    for st, r, p, m, c in cur.fetchall():
        emit("  %-5s %11s %11s %11s %15s"
             % (st, f"{r:,}", f"{p:,}", f"{m:,}", f"{c:,.0f}"))

    emit()
    emit("=== THE TWO STATE FACTS, side by side ===")
    cur.execute("""SELECT fill_state,
                      count(DISTINCT npi) providers,
                      count(DISTINCT npi) FILTER (WHERE NOT state_differs) same,
                      count(DISTINCT npi) FILTER (WHERE state_differs) differs,
                      count(DISTINCT npi) FILTER (WHERE billing_state IS NULL) no_billing
                   FROM provider_molecule_v2 GROUP BY 1 ORDER BY 1""")
    emit("  %-5s %11s %10s %10s %12s"
         % ("fill", "providers", "same", "differs", "no billing"))
    for st, p, sm, df, nb in cur.fetchall():
        emit("  %-5s %11s %10s %10s %12s"
             % (st, f"{p:,}", f"{sm:,}", f"{df:,}", f"{nb:,}"))
    emit()
    emit("  claim volume prescribed INTO a state by an out-of-state office:")
    cur.execute("""SELECT fill_state, sum(tot_clms), sum(tot_drug_cst)
                   FROM provider_molecule_v2 WHERE state_differs
                   GROUP BY 1 ORDER BY 1""")
    for st, c, d in cur.fetchall():
        emit("    %-4s claims=%s cost=$%s" % (st, f"{c:,.0f}", f"{d:,.0f}"))

    emit()
    emit("=== NUCC SPECIALTY NOW ON THE ROW ===")
    cur.execute("""SELECT count(DISTINCT npi) FILTER (WHERE primary_nucc IS NOT NULL),
                          count(DISTINCT npi) FILTER (WHERE primary_nucc IS NULL)
                   FROM provider_molecule_v2""")
    withn, without = cur.fetchone()
    emit("  providers WITH a NUCC code=%s  WITHOUT=%s" % (f"{withn:,}", f"{without:,}"))
    emit()
    emit("  top specialties by claim volume, each state:")
    for st in ("DE", "AL"):
        cur.execute("""SELECT primary_nucc, max(primary_nucc_label),
                              count(DISTINCT npi), sum(tot_clms)
                       FROM provider_molecule_v2
                       WHERE fill_state=%s AND primary_nucc IS NOT NULL
                       GROUP BY 1 ORDER BY 4 DESC LIMIT 6""", (st,))
        emit("    %s:" % st)
        for code, lab, n_, c in cur.fetchall():
            emit("      %-12s %-42s prov=%-6s claims=%s"
                 % (code, str(lab)[:42], f"{n_:,}", f"{c:,.0f}"))

    emit()
    emit("=== WHY Prscrbr_Type CANNOT BE THE SPECIALTY ===")
    cur.execute("""SELECT claim_specialty_text, primary_nucc_label,
                          count(DISTINCT npi)
                   FROM provider_molecule_v2
                   WHERE primary_nucc_label IS NOT NULL
                     AND lower(claim_specialty_text) <> lower(primary_nucc_label)
                   GROUP BY 1,2 ORDER BY 3 DESC LIMIT 8""")
    emit("  %-26s %-40s %s" % ("claim text", "primary NUCC label", "providers"))
    for ty, lab, n_ in cur.fetchall():
        emit("  %-26s %-40s %s" % (str(ty)[:26], str(lab)[:40], f"{n_:,}"))

    emit()
    emit("=== PRACTICE ADDRESSES: aggregated, display only ===")
    cur.execute("""SELECT count(*) FILTER (WHERE n_practice_addresses=1),
                          count(*) FILTER (WHERE n_practice_addresses>1),
                          max(n_practice_addresses)
                   FROM (SELECT DISTINCT npi, n_practice_addresses
                         FROM provider_molecule_v2) x""")
    a, b, mx = cur.fetchone()
    emit("  providers with 1 practice address=%s  with many=%s  worst=%s"
         % (f"{a:,}", f"{b:,}", mx))
    emit("  carried as an array and a count, so the file stays one row per "
         "(provider, molecule) no matter how many addresses a provider has")
    con.close()
    emit()
    emit("written to " + OUT)


main()
