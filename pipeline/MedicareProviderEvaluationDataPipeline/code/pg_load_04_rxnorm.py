"""Stage 4 -- RxNorm RXNCONSO and RXNREL, flat.

This is the vocabulary that makes the join deterministic instead of fuzzy:

  RXNCONSO  every concept name with its TERM TYPE -- IN (ingredient),
            PIN (precise ingredient, i.e. a salt form), BN (brand name),
            SBD/SCD (dose forms) -- so "Gabapentin" can be KNOWN to be an
            ingredient and "Lipitor" KNOWN to be a brand, rather than guessed.

  RXNREL    the relationships between them -- tradename_of,
            has_precise_ingredient, consists_of -- so a brand resolves to its
            ingredient by a recorded fact rather than string similarity.

Both archived copies of RXNCONSO are loaded, each row tagged with the blob it
came from. Two runs archived files of 132.8 MB and 30.7 MB; one is very likely
a truncated fetch, and loading both lets the row counts say which rather than
me choosing.

RRF is pipe-delimited with a TRAILING pipe, so a split yields one extra empty
field. Columns are per the UMLS RxNorm technical documentation.
"""
import csv
import io
import json
import os
import sys
import subprocess
import time
import zipfile

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
DB = "medicare_join"
LOG = r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline\code\pg_load_progress.txt"
SOURCES = r"C:\DataExperiments\Sources"
ACCOUNT = "stchpipelinedev"
CONTAINER = "dev-pipeline-sources"

RXNCONSO_COLS = ["rxcui", "lat", "ts", "lui", "stt", "sui", "ispref", "rxaui",
                 "saui", "scui", "sdui", "sab", "tty", "code", "str_", "srl",
                 "suppress", "cvf"]
RXNREL_COLS = ["rxcui1", "rxaui1", "stype1", "rel", "rxcui2", "rxaui2",
               "stype2", "rela", "rui", "srui", "sab", "sl", "rg", "dir",
               "suppress", "cvf"]


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


def open_rrf(path):
    with open(path, "rb") as fh:
        magic = fh.read(2)
    if magic == b"PK":
        zf = zipfile.ZipFile(path)
        inner = [n for n in zf.namelist() if n.upper().endswith(".RRF")]
        name = inner[0] if inner else zf.namelist()[0]
        note("  zip member %s" % name)
        return io.TextIOWrapper(zf.open(name), encoding="utf-8",
                                errors="replace", newline="")
    return open(path, encoding="utf-8", errors="replace", newline="")


def load(cur, con, prefix, table, cols):
    cur.execute("DROP TABLE IF EXISTS %s" % table)
    cur.execute("CREATE TABLE %s (load_seq bigserial PRIMARY KEY, "
                "source_blob text, %s)"
                % (table, ", ".join(f"{c} text" for c in cols)))
    con.commit()
    note("created %s" % table)

    total = 0
    for blob in blob_list(prefix):
        path = fetch(blob)
        note("loading %s -> %s" % (blob, table))
        n = 0
        buf = io.StringIO()
        w = csv.writer(buf)
        fh = open_rrf(path)
        for line in fh:
            line = line.rstrip("\r\n")
            if not line:
                continue
            parts = line.split("|")
            if parts and parts[-1] == "":
                parts = parts[:-1]
            if len(parts) < len(cols):
                parts = parts + [""] * (len(cols) - len(parts))
            w.writerow([blob] + parts[:len(cols)])
            n += 1
            if n % 500_000 == 0:
                buf.seek(0)
                cur.copy_expert("COPY %s (source_blob,%s) FROM STDIN WITH (FORMAT csv)"
                                % (table, ",".join(cols)), buf)
                con.commit()
                note("    %s rows" % f"{n:,}")
                buf = io.StringIO()
                w = csv.writer(buf)
        fh.close()
        buf.seek(0)
        cur.copy_expert("COPY %s (source_blob,%s) FROM STDIN WITH (FORMAT csv)"
                        % (table, ",".join(cols)), buf)
        con.commit()
        note("  %s: %s rows" % (blob, f"{n:,}"))
        total += n
    note("%s total rows=%s" % (table, f"{total:,}"))
    return total


def main():
    con = connect()
    cur = con.cursor()

    load(cur, con, "rxnorm/", "rxnconso", RXNCONSO_COLS)
    load(cur, con, "rxnorm_rel/", "rxnrel", RXNREL_COLS)

    note("indexing")
    for ddl in (
        "CREATE INDEX rxnconso_rxcui ON rxnconso (rxcui)",
        "CREATE INDEX rxnconso_tty ON rxnconso (tty)",
        "CREATE INDEX rxnconso_str_upper ON rxnconso (upper(str_))",
        "CREATE INDEX rxnconso_tty_str ON rxnconso (tty, upper(str_))",
        "CREATE INDEX rxnrel_rxcui1 ON rxnrel (rxcui1)",
        "CREATE INDEX rxnrel_rxcui2 ON rxnrel (rxcui2)",
        "CREATE INDEX rxnrel_rela ON rxnrel (rela)",
    ):
        cur.execute(ddl)
        con.commit()

    note("--- which archived RXNCONSO copy is complete ---")
    cur.execute("""SELECT source_blob, count(*), count(DISTINCT rxcui)
                   FROM rxnconso GROUP BY source_blob ORDER BY 2 DESC""")
    for b, n, d in cur.fetchall():
        note("  %-48s rows=%-12s distinct_rxcui=%s" % (b, f"{n:,}", f"{d:,}"))

    note("--- term types present (English, unsuppressed) ---")
    cur.execute("""SELECT tty, count(*) FROM rxnconso
                   WHERE lat='ENG' AND suppress='N' GROUP BY tty
                   ORDER BY 2 DESC LIMIT 15""")
    for t, n in cur.fetchall():
        note("  %-8s %s" % (t, f"{n:,}"))

    note("--- relationships that matter for brand -> ingredient ---")
    cur.execute("""SELECT rela, count(*) FROM rxnrel
                   WHERE rela IN ('tradename_of','has_tradename',
                                  'has_precise_ingredient','precise_ingredient_of',
                                  'ingredient_of','has_ingredient',
                                  'consists_of','constitutes','has_form','form_of')
                   GROUP BY rela ORDER BY 2 DESC""")
    for r, n in cur.fetchall():
        note("  %-26s %s" % (r, f"{n:,}"))
    con.close()
    note("STAGE 4 DONE")


main()
