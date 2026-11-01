"""Stage 3 -- the COMPLETE set of distinct Part D (brand, generic) pairs.

28M rows collapse to a few thousand distinct drug-name pairs. Rows are what the
file has; pairs are what the join needs. So this streams the whole archived
Part D source once and loads only the distinct pairs, each carrying the volume
behind it -- full pair coverage and true claim weighting without 28M rows in
Postgres.

Also marks which pairs appear in the first 500,000 rows already loaded in
`partd`. Those are the DEVELOPMENT set. Every pair that appears only later is
the HOLDOUT: the long tail of rare drugs, which is where resolution actually
fails, so an algorithm measured only on the development set would flatter
itself.

Source is Azure, cached under C:\\DataExperiments\\Sources.
"""
import csv
import io
import json
import os
import subprocess
import sys
import time
import zipfile

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
DB = "medicare_join"
LOG = r"C:\CHATHealthyLLC\DataExperiments\pg_load_progress.txt"
SOURCES = r"C:\DataExperiments\Sources"
ACCOUNT = "stchpipelinedev"
CONTAINER = "dev-pipeline-sources"


def note(msg):
    line = time.strftime("%H:%M:%S ") + msg
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    _log.info("%s", line)


def _az():
    import shutil
    for c in ("az", "az.cmd", "az.bat"):
        p = shutil.which(c)
        if p:
            return p
    raise ChatHealthyException("runtime_error", "az not found")


AZ = _az()


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname=DB)


def blob_list(prefix):
    out = subprocess.run(
        [AZ, "storage", "blob", "list", "--account-name", ACCOUNT,
         "--container-name", CONTAINER, "--auth-mode", "login",
         "--prefix", prefix, "--query", "[].name", "-o", "json"],
        capture_output=True, text=True, timeout=600)
    if out.returncode != 0:
        raise ChatHealthyException("runtime_error", "list failed: " + out.stderr[:400])
    return sorted(json.loads(out.stdout))


def fetch(name):
    dest = os.path.join(SOURCES, name.replace("/", os.sep))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        note("  cached %s (%.0f MB)" % (dest, os.path.getsize(dest) / 1e6))
        return dest
    note("  downloading %s" % name)
    out = subprocess.run(
        [AZ, "storage", "blob", "download", "--account-name", ACCOUNT,
         "--container-name", CONTAINER, "--auth-mode", "login",
         "--name", name, "--file", dest, "--no-progress", "-o", "none"],
        capture_output=True, text=True, timeout=7200)
    if out.returncode != 0:
        raise ChatHealthyException("runtime_error", "download failed: " + out.stderr[:400])
    note("  got %.0f MB" % (os.path.getsize(dest) / 1e6))
    return dest


def open_text(path):
    """The archived blob is whatever was fetched: raw CSV or a zip of one."""
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic[:2] == b"PK":
        zf = zipfile.ZipFile(path)
        inner = zf.namelist()[0]
        note("  zip containing %s" % inner)
        return io.TextIOWrapper(zf.open(inner), encoding="utf-8-sig",
                                errors="replace", newline="")
    return open(path, encoding="utf-8-sig", errors="replace", newline="")


def _num(s):
    s = (s or "").strip().replace(",", "").replace("$", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def main():
    con = connect()
    con.autocommit = True
    cur = con.cursor()
    cur.execute("DROP TABLE IF EXISTS partd_pair")
    cur.execute("""
        CREATE TABLE partd_pair (
          load_seq    bigserial PRIMARY KEY,
          brnd_name   text,
          gnrc_name   text,
          src_rows    bigint,
          tot_clms    double precision,
          tot_drug_cst double precision,
          in_dev_set  boolean
        )""")
    note("created partd_pair")

    names = blob_list("medicare_partd/")
    note("medicare_partd blobs: %d -> %s" % (len(names), names))

    agg = {}
    rows = 0
    t0 = time.time()
    for blob in names:
        path = fetch(blob)
        note("streaming %s" % blob)
        fh = open_text(path)
        reader = csv.reader(fh)
        header = next(reader)
        note("  header (%d cols): %s" % (len(header), ",".join(header)))
        idx = {h.strip().lower(): i for i, h in enumerate(header)}
        ib, ig = idx.get("brnd_name"), idx.get("gnrc_name")
        ic, io_ = idx.get("tot_clms"), idx.get("tot_drug_cst")
        if ib is None or ig is None:
            raise ChatHealthyException("runtime_error", "brand/generic columns absent from header")
        for row in reader:
            rows += 1
            try:
                k = (row[ib].strip(), row[ig].strip())
            except IndexError:
                continue
            a = agg.get(k)
            if a is None:
                agg[k] = a = [0, 0.0, 0.0]
            a[0] += 1
            if ic is not None and ic < len(row):
                a[1] += _num(row[ic])
            if io_ is not None and io_ < len(row):
                a[2] += _num(row[io_])
            if rows % 2_000_000 == 0:
                note("  rows=%,d pairs=%d  %.0fs" % (rows, len(agg), time.time() - t0)
                     if False else
                     "  rows=%s pairs=%d  %.0fs" % (f"{rows:,}", len(agg),
                                                    time.time() - t0))
        fh.close()

    note("streamed rows=%s distinct pairs=%s in %.0fs"
         % (f"{rows:,}", f"{len(agg):,}", time.time() - t0))

    buf = io.StringIO()
    w = csv.writer(buf)
    for (b, g), (n, c, d) in agg.items():
        w.writerow([b, g, n, c, d])
    buf.seek(0)
    cur.execute("BEGIN")
    cur.copy_expert(
        "COPY partd_pair (brnd_name,gnrc_name,src_rows,tot_clms,tot_drug_cst) "
        "FROM STDIN WITH (FORMAT csv)", buf)
    cur.execute("COMMIT")

    cur.execute("""
        UPDATE partd_pair p SET in_dev_set = EXISTS (
          SELECT 1 FROM partd d
          WHERE d.brnd_name = p.brnd_name AND d.gnrc_name = p.gnrc_name)""")
    cur.execute("CREATE INDEX partd_pair_brnd ON partd_pair (brnd_name)")
    cur.execute("CREATE INDEX partd_pair_dev ON partd_pair (in_dev_set)")

    cur.execute("""SELECT in_dev_set, count(*), sum(src_rows), sum(tot_clms)
                   FROM partd_pair GROUP BY in_dev_set ORDER BY 1 DESC NULLS LAST""")
    note("development / holdout split:")
    for dev, n, r, c in cur.fetchall():
        label = "DEV (in first 500K rows)" if dev else "HOLDOUT (tail only)"
        note("  %-26s pairs=%-6s rows=%-12s claims=%s"
             % (label, f"{n:,}", f"{r:,}", f"{c:,.0f}"))
    cur.execute("SELECT count(DISTINCT brnd_name), count(DISTINCT gnrc_name) FROM partd_pair")
    b, g = cur.fetchone()
    note("distinct brands=%s distinct generics=%s" % (f"{b:,}", f"{g:,}"))
    cur.execute("SELECT sum(tot_clms), sum(tot_drug_cst) FROM partd_pair")
    c, d = cur.fetchone()
    note("total claims=%s total cost=$%s" % (f"{c:,.0f}", f"{d:,.0f}"))
    con.close()
    note("STAGE 3 DONE")


main()
