"""Execution step 1 -- replace partd with the COMPLETE Delaware and Alabama claims.

partd previously held the first 500,000 rows of the national file. That is a
development slice, not a state-complete one: it contains some DE and AL rows
but not all of them, so any provider-level figure computed from it would be a
partial sample of each provider. The experiment is scoped to all claims for
both states, so the table is rebuilt filtered to DE and AL.

Kept: partd_pair, the 3,159 national distinct pairs with the dev/holdout flag.
T1 is specified against both the national universe and the DE+AL subset, so the
national pair table remains the context.

Added: partd_pair_state, one row per (state, Brnd_Name, Gnrc_Name), so Delaware
and Alabama are never averaged together at any later hop.

Expected from the streamed counts: DE 86,621 rows / 3,601 providers,
AL 467,696 rows / 15,158 providers, 554,317 rows combined, 1,910 distinct pairs.
Those are asserted at the end; a mismatch stops the step.
"""
import sys
import csv
import io
import os
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
DB = "medicare_join"
LOG = r"C:\CHATHealthyLLC\DataExperiments\exec_progress.txt"
PARTD = r"C:\DataExperiments\Sources\medicare_partd\45387cb165f8c7a6\medicare_partd"
STATES = ("DE", "AL")

PARTD_COLS = [
    "prscrbr_npi", "prscrbr_last_org_name", "prscrbr_first_name",
    "prscrbr_city", "prscrbr_state_abrvtn", "prscrbr_state_fips",
    "prscrbr_type", "prscrbr_type_src", "brnd_name", "gnrc_name",
    "tot_clms", "tot_30day_fills", "tot_day_suply", "tot_drug_cst",
    "tot_benes", "ge65_sprsn_flag", "ge65_tot_clms", "ge65_tot_30day_fills",
    "ge65_tot_day_suply", "ge65_tot_drug_cst", "ge65_bene_sprsn_flag",
    "ge65_tot_benes",
]

EXPECT = {"DE": (86_621, 3_601), "AL": (467_696, 15_158)}


def note(msg):
    line = time.strftime("%H:%M:%S ") + msg
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    _log.info("%s", line)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname=DB)


def main():
    con = connect()
    con.autocommit = True
    cur = con.cursor()
    cur.execute("SET statement_timeout='1800s'")

    cur.execute("SELECT count(*) FROM partd")
    note("purging partd: %s rows (the national 500K development slice)"
         % f"{cur.fetchone()[0]:,}")
    cur.execute("DROP TABLE IF EXISTS partd")
    cols = ",\n          ".join(f"{c} text" for c in PARTD_COLS)
    cur.execute(f"""
        CREATE TABLE partd (
          load_seq bigserial PRIMARY KEY,
          {cols}
        )""")
    note("partd recreated, empty")

    note("streaming %s filtered to %s" % (os.path.basename(PARTD), "/".join(STATES)))
    con.autocommit = False
    buf = io.StringIO()
    w = csv.writer(buf)
    scanned = kept = pending = 0
    t0 = time.time()
    with open(PARTD, encoding="utf-8-sig", errors="replace", newline="") as fh:
        rdr = csv.reader(fh)
        header = next(rdr)
        idx = {c.strip().lower(): i for i, c in enumerate(header)}
        if len(header) != len(PARTD_COLS):
            raise ChatHealthyException("runtime_error", "header has %d columns, expected %d"
                             % (len(header), len(PARTD_COLS)))
        st = idx["prscrbr_state_abrvtn"]
        for row in rdr:
            scanned += 1
            if len(row) <= st or row[st].strip() not in STATES:
                continue
            w.writerow(row[:len(PARTD_COLS)])
            kept += 1
            pending += 1
            if pending >= 50_000:
                buf.seek(0)
                cur.copy_expert("COPY partd (%s) FROM STDIN WITH (FORMAT csv)"
                                % ",".join(PARTD_COLS), buf)
                con.commit()
                note("  scanned=%s kept=%s  %.0fs"
                     % (f"{scanned:,}", f"{kept:,}", time.time() - t0))
                buf = io.StringIO()
                w = csv.writer(buf)
                pending = 0
    if pending:
        buf.seek(0)
        cur.copy_expert("COPY partd (%s) FROM STDIN WITH (FORMAT csv)"
                        % ",".join(PARTD_COLS), buf)
        con.commit()
    note("streamed %s rows, kept %s" % (f"{scanned:,}", f"{kept:,}"))
    con.autocommit = True

    for ddl in (
        "CREATE INDEX partd_state ON partd (prscrbr_state_abrvtn)",
        "CREATE INDEX partd_npi ON partd (prscrbr_npi)",
        "CREATE INDEX partd_pair_k ON partd (brnd_name, gnrc_name)",
        "CREATE INDEX partd_state_npi ON partd (prscrbr_state_abrvtn, prscrbr_npi)",
    ):
        cur.execute(ddl)
    cur.execute("ANALYZE partd")
    note("indexed")

    # ---- the state-scoped pair table: DE and AL never averaged -------------
    cur.execute("DROP TABLE IF EXISTS partd_pair_state")
    cur.execute("""
        CREATE TABLE partd_pair_state AS
        SELECT prscrbr_state_abrvtn AS state,
               brnd_name, gnrc_name,
               count(*)                                            AS src_rows,
               count(DISTINCT prscrbr_npi)                          AS providers,
               sum(NULLIF(tot_clms,'')::double precision)           AS tot_clms,
               sum(NULLIF(tot_drug_cst,'')::double precision)       AS tot_drug_cst,
               sum(NULLIF(tot_day_suply,'')::double precision)      AS tot_day_suply,
               sum(NULLIF(tot_benes,'')::double precision)          AS tot_benes,
               count(*) FILTER (WHERE NULLIF(tot_benes,'') IS NULL) AS benes_suppressed
        FROM partd
        GROUP BY 1,2,3""")
    cur.execute("CREATE INDEX pps_k ON partd_pair_state (brnd_name, gnrc_name)")
    cur.execute("CREATE INDEX pps_state ON partd_pair_state (state)")
    cur.execute("ANALYZE partd_pair_state")

    note("")
    note("=== VERIFICATION against the streamed counts ===")
    cur.execute("""SELECT prscrbr_state_abrvtn, count(*), count(DISTINCT prscrbr_npi),
                          count(DISTINCT (brnd_name, gnrc_name)),
                          sum(NULLIF(tot_clms,'')::double precision),
                          sum(NULLIF(tot_drug_cst,'')::double precision)
                   FROM partd GROUP BY 1 ORDER BY 1""")
    ok = True
    for state, rows, provs, pairs, clms, cost in cur.fetchall():
        exp_rows, exp_provs = EXPECT[state]
        match = (rows == exp_rows and provs == exp_provs)
        ok = ok and match
        note("  %s rows=%s (expect %s) providers=%s (expect %s) pairs=%s "
             "claims=%s cost=$%s  %s"
             % (state, f"{rows:,}", f"{exp_rows:,}", f"{provs:,}",
                f"{exp_provs:,}", f"{pairs:,}", f"{clms:,.0f}", f"{cost:,.0f}",
                "OK" if match else "MISMATCH"))
    cur.execute("SELECT count(*), count(DISTINCT prscrbr_npi) FROM partd")
    r, p = cur.fetchone()
    note("  combined rows=%s providers=%s (expect 554,317 / 18,759)"
         % (f"{r:,}", f"{p:,}"))
    cur.execute("SELECT count(*) FROM (SELECT DISTINCT brnd_name, gnrc_name FROM partd) d")
    dp = cur.fetchone()[0]
    note("  distinct pairs across both states=%s (expect 1,910)" % f"{dp:,}")

    cur.execute("SELECT count(*) FROM partd_pair")
    note("  partd_pair retained (national universe)=%s rows" % f"{cur.fetchone()[0]:,}")
    cur.execute("SELECT state, count(*) FROM partd_pair_state GROUP BY 1 ORDER BY 1")
    for s, n in cur.fetchall():
        note("  partd_pair_state %s=%s pairs" % (s, f"{n:,}"))

    if not (ok and r == 554_317 and p == 18_759 and dp == 1_910):
        raise ChatHealthyException("runtime_error", "VERIFICATION FAILED -- counts do not match the "
                         "independently streamed figures; stopping")
    note("")
    note("EXEC STEP 1 DONE -- partd is now DE+AL complete and verified")
    con.close()


main()
