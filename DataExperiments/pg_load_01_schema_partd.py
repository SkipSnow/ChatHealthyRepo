"""Stage 1 -- create the analysis database, the flat schema, and load Part D.

Relational, flat, one table per source. Every table carries load_seq: a bigserial
that IS the sequence of the load, nothing more.

Part D is loaded whole. The HTTP stream is read to exhaustion; no row count
stops it.
"""
import csv
import io
import json
import os
import sys
import time
import urllib.request

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
DB = "medicare_join"
LOG = r"C:\CHATHealthyLLC\_oneshots\pg_load_progress.txt"


def note(msg):
    line = time.strftime("%H:%M:%S ") + msg
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    _log.info("%s", line)


def pw():
    with open(PW_FILE, encoding="utf-8") as fh:
        return fh.read().strip()


def connect(dbname):
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw(), dbname=dbname)


def ensure_db():
    con = connect("postgres")
    con.autocommit = True
    with con.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB,))
        if cur.fetchone():
            note(f"database {DB} exists")
        else:
            cur.execute(f'CREATE DATABASE "{DB}"')
            note(f"created database {DB}")
    con.close()


# The 23 columns CMS publishes on "by Provider and Drug", in the order the
# CSV header gives them. Loaded as text: no coercion, no interpretation.
PARTD_COLS = [
    "prscrbr_npi", "prscrbr_last_org_name", "prscrbr_first_name",
    "prscrbr_city", "prscrbr_state_abrvtn", "prscrbr_state_fips",
    "prscrbr_type", "prscrbr_type_src", "brnd_name", "gnrc_name",
    "tot_clms", "tot_30day_fills", "tot_day_suply", "tot_drug_cst",
    "tot_benes", "ge65_sprsn_flag", "ge65_tot_clms", "ge65_tot_30day_fills",
    "ge65_tot_day_suply", "ge65_tot_drug_cst", "ge65_bene_sprsn_flag",
    "ge65_tot_benes",
]


def schema():
    con = connect(DB)
    con.autocommit = True
    with con.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS partd")
        cols = ",\n  ".join(f"{c} text" for c in PARTD_COLS)
        cur.execute(f"""
            CREATE TABLE partd (
              load_seq bigserial PRIMARY KEY,
              {cols}
            )""")
        note("created table partd (%d text columns + load_seq)" % len(PARTD_COLS))
    con.close()


def csv_url():
    cat = json.loads(urllib.request.urlopen(
        "https://data.cms.gov/data.json", timeout=180).read())
    best = None
    for ds in cat.get("dataset", []):
        t = ds.get("title", "")
        if t.startswith("Medicare Part D Prescribers - by Provider and Drug"):
            if best is None or t > best.get("title", ""):
                best = ds
    note("newest release: " + best["title"])
    for dist in best["distribution"]:
        if dist.get("format") == "CSV":
            url = dist.get("downloadURL") or dist.get("accessURL")
            note("csv: " + url)
            return url
    raise ChatHealthyException("runtime_error", "no CSV distribution found")


def load_partd(url):
    con = connect(DB)
    con.autocommit = False
    cur = con.cursor()

    buf = io.StringIO()
    writer = csv.writer(buf)
    rows = 0
    header = None
    pending = 0

    note("streaming the whole CSV")
    with urllib.request.urlopen(url, timeout=3600) as resp:
        text = io.TextIOWrapper(resp, encoding="utf-8-sig", errors="replace",
                                newline="")
        reader = csv.reader(text)
        for row in reader:
            if header is None:
                header = row
                note("CSV header (%d cols): %s" % (len(header), ",".join(header)))
                if len(header) != len(PARTD_COLS):
                    note("WARNING: header column count %d != expected %d"
                         % (len(header), len(PARTD_COLS)))
                continue
            writer.writerow(row[:len(PARTD_COLS)])
            rows += 1
            pending += 1
            if pending >= 50_000:
                buf.seek(0)
                cur.copy_expert(
                    "COPY partd (%s) FROM STDIN WITH (FORMAT csv)"
                    % ",".join(PARTD_COLS), buf)
                con.commit()
                note(f"  loaded {rows:,}")
                buf = io.StringIO()
                writer = csv.writer(buf)
                pending = 0
    if pending:
        buf.seek(0)
        cur.copy_expert(
            "COPY partd (%s) FROM STDIN WITH (FORMAT csv)"
            % ",".join(PARTD_COLS), buf)
        con.commit()
        note(f"  loaded {rows:,}")

    cur.execute("SELECT count(*), min(load_seq), max(load_seq) FROM partd")
    n, lo, hi = cur.fetchone()
    note(f"partd rows={n:,} load_seq {lo}..{hi}")
    cur.execute("CREATE INDEX partd_brnd_gnrc ON partd (brnd_name, gnrc_name)")
    cur.execute("CREATE INDEX partd_brnd ON partd (brnd_name)")
    con.commit()
    note("indexed partd on (brnd_name, gnrc_name) and (brnd_name)")

    cur.execute("""SELECT count(*) FROM (
                     SELECT DISTINCT brnd_name, gnrc_name FROM partd) d""")
    note(f"distinct (brnd_name, gnrc_name) pairs in the loaded slice: {cur.fetchone()[0]:,}")
    con.close()


if __name__ == "__main__":
    if os.path.exists(LOG):
        os.remove(LOG)
    ensure_db()
    schema()
    load_partd(csv_url())
    note("STAGE 1 DONE")
