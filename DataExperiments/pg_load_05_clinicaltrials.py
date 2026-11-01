"""Stage 5 -- ClinicalTrials.gov as a third identity bridge to the molecule.

RxNorm and the FDA label have given up most of what they hold. The residual is
concentrated where neither covers well: brand names (90.05% resolved against
99.79% for generics) and 670 pairs RxNorm has never heard of at all.

A trial record names its intervention by the name the sponsor used AND carries
otherNames -- an explicit alias list. That makes the registry an independent
brand-to-molecule bridge, and ChatHealthy already consumes this source.

Downloads the official complete corpus (api/v2/studies/download?format=json.zip,
verified HTTP 200 application/zip), inspects its structure before parsing it,
then loads interventions flat with a load_seq.

Data lands in C:\\DataExperiments\\Sources\\clinicaltrials.
"""
import csv
import io
import json
import os
import sys
import time
import urllib.request
import zipfile

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
LOG = r"C:\CHATHealthyLLC\DataExperiments\pg_load_progress.txt"
SRC_DIR = r"C:\DataExperiments\Sources\clinicaltrials"
ZIP_PATH = os.path.join(SRC_DIR, "ctg-studies.json.zip")
URL = "https://clinicaltrials.gov/api/v2/studies/download?format=json.zip"


def note(msg):
    line = time.strftime("%H:%M:%S ") + msg
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    _log.info("%s", line)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def download():
    os.makedirs(SRC_DIR, exist_ok=True)
    if os.path.exists(ZIP_PATH) and os.path.getsize(ZIP_PATH) > 1_000_000:
        note("cached %s (%.0f MB)" % (ZIP_PATH, os.path.getsize(ZIP_PATH) / 1e6))
        return ZIP_PATH
    note("downloading the complete ClinicalTrials.gov corpus")
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    got = 0
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=7200) as src, open(ZIP_PATH, "wb") as dst:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            dst.write(chunk)
            got += len(chunk)
            if got % (100 << 20) < (1 << 20):
                note("   %.0f MB in %.0fs" % (got / 1e6, time.time() - t0))
    note("downloaded %.0f MB in %.0fs" % (got / 1e6, time.time() - t0))
    return ZIP_PATH


def inspect(path):
    """Look at the archive before parsing it."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        note("archive members: %s" % f"{len(names):,}")
        for n in names[:5]:
            note("   %s" % n)
        first = names[0]
        with zf.open(first) as fh:
            raw = fh.read()
        note("first member %s is %s bytes" % (first, f"{len(raw):,}"))
        doc = json.loads(raw)
        if isinstance(doc, list):
            note("   member is a LIST of %s studies" % f"{len(doc):,}")
            doc = doc[0]
        note("   top-level keys: %s" % list(doc.keys())[:8])
        ps = doc.get("protocolSection", {})
        note("   protocolSection keys: %s" % list(ps.keys())[:12])
        ai = ps.get("armsInterventionsModule", {})
        note("   interventions sample: %s"
             % json.dumps((ai.get("interventions") or [])[:2])[:300])
        return names


def load(path, names):
    con = connect()
    cur = con.cursor()
    cur.execute("DROP TABLE IF EXISTS ct_intervention")
    cur.execute("""
        CREATE TABLE ct_intervention (
          load_seq   bigserial PRIMARY KEY,
          nct_id     text,
          ivtype     text,
          name       text,
          other_name text
        )""")
    con.commit()
    note("created ct_intervention")

    buf = io.StringIO()
    w = csv.writer(buf)
    studies = rows = 0
    t0 = time.time()
    with zipfile.ZipFile(path) as zf:
        for member in names:
            if not member.lower().endswith(".json"):
                continue
            with zf.open(member) as fh:
                try:
                    doc = json.loads(fh.read())
                except Exception:
                    continue
            docs = doc if isinstance(doc, list) else [doc]
            for d in docs:
                studies += 1
                ps = d.get("protocolSection", {})
                nct = (ps.get("identificationModule") or {}).get("nctId") or ""
                for iv in ((ps.get("armsInterventionsModule") or {})
                           .get("interventions") or []):
                    nm = (iv.get("name") or "").strip()
                    tp = (iv.get("type") or "").strip()
                    others = iv.get("otherNames") or [None]
                    for on in others:
                        w.writerow([nct, tp, nm, (on or "").strip()])
                        rows += 1
                if studies % 50_000 == 0:
                    buf.seek(0)
                    cur.copy_expert("COPY ct_intervention "
                                    "(nct_id,ivtype,name,other_name) "
                                    "FROM STDIN WITH (FORMAT csv)", buf)
                    con.commit()
                    note("   studies=%s intervention_rows=%s  %.0fs"
                         % (f"{studies:,}", f"{rows:,}", time.time() - t0))
                    buf = io.StringIO()
                    w = csv.writer(buf)
    buf.seek(0)
    cur.copy_expert("COPY ct_intervention (nct_id,ivtype,name,other_name) "
                    "FROM STDIN WITH (FORMAT csv)", buf)
    con.commit()
    note("loaded studies=%s intervention_rows=%s" % (f"{studies:,}", f"{rows:,}"))

    for ddl in (
        "CREATE INDEX ct_name_upper ON ct_intervention (upper(name))",
        "CREATE INDEX ct_other_upper ON ct_intervention (upper(other_name))",
        "CREATE INDEX ct_type ON ct_intervention (ivtype)",
    ):
        cur.execute(ddl)
    cur.execute("ANALYZE ct_intervention")
    con.commit()
    note("indexed")

    cur.execute("""SELECT ivtype, count(*), count(DISTINCT upper(name))
                   FROM ct_intervention GROUP BY 1 ORDER BY 2 DESC LIMIT 12""")
    note("intervention types:")
    for t, n, d in cur.fetchall():
        note("   %-16s rows=%-9s distinct_names=%s" % (t, f"{n:,}", f"{d:,}"))
    cur.execute("SELECT count(*) FROM ct_intervention WHERE other_name <> ''")
    note("rows carrying an alias (otherNames): %s" % f"{cur.fetchone()[0]:,}")
    con.close()


def main():
    path = download()
    names = inspect(path)
    load(path, names)
    note("STAGE 5 DONE")


main()
