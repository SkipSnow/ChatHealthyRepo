"""Execution step 4 -- load the ICD-side vocabularies.

Four already-declared pipeline sources, all archived locally:

  icd10_cm                CMS. icd10cm_codes_2026.txt (DOTLESS code + description)
                          and icd10cm_order_2026.txt, whose validity flag is the
                          mechanical level-3 vs level-4 distinction:
                            0 = category header  (level 3, e.g. E11)
                            1 = billable leaf    (level 4+, e.g. E11.9)
  ccsr                    AHRQ HCUP DXCCSR v2026-1. ICD-10-CM -> clinical
                          categories and body system.
  medicare_coverage_icd10 CMS Coverage Articles, article_x_icd10_covered.csv.
                          icd10_code_id is DOTTED.
  medicare_coverage_hcpc  the SAME all_article.zip, article_x_hcpc_code.csv.
                          Shares article_id, which is the procedure<->diagnosis
                          bridge.

CODE FORMAT: coverage is dotted, ICD-10-CM and CCSR are dotless. They will not
join until one form is canonical. DOTTED is chosen -- it is the published human
form and the form the coverage data already uses -- and every source is
normalised to it at load. A dotless code CCCXXXX becomes CCC.XXXX.
"""
import csv
import io
import os
import sys
import time
import zipfile

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline\code\exec_04_icd_result.txt"
SRC = r"C:\DataExperiments\Sources"


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def dot(code):
    """Canonical DOTTED form. A000 -> A00.0 ; A00 -> A00 ; already-dotted kept."""
    c = (code or "").strip().upper().replace(" ", "")
    if not c or "." in c:
        return c
    return c if len(c) <= 3 else c[:3] + "." + c[3:]


def find(prefix, suffix=None):
    base = os.path.join(SRC, prefix)
    for root, _dirs, files in os.walk(base):
        for f in files:
            if suffix is None or f.endswith(suffix):
                return os.path.join(root, f)
    raise ChatHealthyException("runtime_error", "not found under " + base)


def load_icd10cm(con, cur):
    path = find("icd10_cm")
    emit("icd10_cm archive: %s (%.1f MB)" % (os.path.basename(path),
                                             os.path.getsize(path) / 1e6))
    cur.execute("DROP TABLE IF EXISTS icd10cm")
    cur.execute("""CREATE TABLE icd10cm (
                     code_dotted text PRIMARY KEY, code_dotless text,
                     level int, is_billable boolean,
                     short_desc text, long_desc text)""")
    rows = {}
    with zipfile.ZipFile(path) as z:
        order_m = [n for n in z.namelist() if n.endswith("icd10cm_order_2026.txt")]
        codes_m = [n for n in z.namelist() if n.endswith("icd10cm_codes_2026.txt")]
        if order_m:
            # fixed width: order(0:5) code(6:13) valid(14:15) short(16:76) long(77:)
            for raw in z.open(order_m[0]):
                ln = raw.decode("utf-8", "replace").rstrip("\r\n")
                if len(ln) < 20:
                    continue
                code = ln[6:13].strip()
                valid = ln[14:15].strip()
                short = ln[16:76].strip()
                long_ = ln[77:].strip()
                d = dot(code)
                rows[d] = [d, code, 3 if len(code) <= 3 else len(code) - 2,
                           valid == "1", short, long_]
            emit("  parsed order file: %s codes" % f"{len(rows):,}")
        if codes_m and not rows:
            for raw in z.open(codes_m[0]):
                ln = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not ln.strip():
                    continue
                parts = ln.split(None, 1)
                code = parts[0]
                d = dot(code)
                rows[d] = [d, code, 3 if len(code) <= 3 else len(code) - 2,
                           True, "", parts[1].strip() if len(parts) > 1 else ""]
            emit("  parsed codes file: %s codes" % f"{len(rows):,}")
    buf = io.StringIO(); w = csv.writer(buf)
    for r in rows.values():
        w.writerow([r[0], r[1], r[2], "true" if r[3] else "false", r[4], r[5]])
    buf.seek(0)
    cur.copy_expert("COPY icd10cm (code_dotted,code_dotless,level,is_billable,"
                    "short_desc,long_desc) FROM STDIN WITH (FORMAT csv)", buf)
    cur.execute("CREATE INDEX icd_dotless ON icd10cm (code_dotless)")
    cur.execute("CREATE INDEX icd_level ON icd10cm (level)")
    cur.execute("CREATE INDEX icd_long_lower ON icd10cm (lower(long_desc))")
    cur.execute("ANALYZE icd10cm")
    con.commit()
    cur.execute("""SELECT count(*), count(*) FILTER (WHERE is_billable),
                          count(*) FILTER (WHERE NOT is_billable),
                          count(*) FILTER (WHERE level=3)
                   FROM icd10cm""")
    a, b, c, d = cur.fetchone()
    emit("  icd10cm=%s  billable(leaf)=%s  category_header=%s  3-char=%s"
         % (f"{a:,}", f"{b:,}", f"{c:,}", f"{d:,}"))
    emit("  the validity flag IS the L3/L4 mechanism: a header is a category, "
         "a billable code is a leaf")


def load_ccsr(con, cur):
    path = find("ccsr")
    cur.execute("DROP TABLE IF EXISTS ccsr")
    cur.execute("""CREATE TABLE ccsr (
                     code_dotted text, code_dotless text, code_desc text,
                     ccsr_default_ip text, ccsr_default_ip_desc text,
                     ccsr_cat1 text, ccsr_cat1_desc text, body_system text)""")
    n = 0
    buf = io.StringIO(); w = csv.writer(buf)
    with zipfile.ZipFile(path) as z:
        m = [x for x in z.namelist() if x.endswith("DXCCSR_v2026-1.csv")][0]
        txt = io.TextIOWrapper(z.open(m), encoding="utf-8", errors="replace",
                               newline="")
        rdr = csv.reader(txt)
        header = [h.strip().strip("'").strip('"') for h in next(rdr)]
        idx = {h: i for i, h in enumerate(header)}

        def g(row, key):
            i = idx.get(key)
            return (row[i].strip().strip("'").strip('"')
                    if i is not None and i < len(row) else "")
        for row in rdr:
            code = g(row, "ICD-10-CM CODE")
            if not code:
                continue
            w.writerow([dot(code), code, g(row, "ICD-10-CM CODE DESCRIPTION"),
                        g(row, "Default CCSR CATEGORY IP"),
                        g(row, "Default CCSR CATEGORY DESCRIPTION IP"),
                        g(row, "CCSR CATEGORY 1"),
                        g(row, "CCSR CATEGORY 1 DESCRIPTION"),
                        row[-1].strip().strip("'").strip('"') if row else ""])
            n += 1
    buf.seek(0)
    cur.copy_expert("COPY ccsr FROM STDIN WITH (FORMAT csv)", buf)
    cur.execute("CREATE INDEX ccsr_dotted ON ccsr (code_dotted)")
    cur.execute("CREATE INDEX ccsr_cat1 ON ccsr (ccsr_cat1)")
    cur.execute("ANALYZE ccsr")
    con.commit()
    cur.execute("SELECT count(*), count(DISTINCT ccsr_cat1), count(DISTINCT body_system) FROM ccsr")
    a, b, c = cur.fetchone()
    emit("ccsr=%s codes  distinct CCSR cat1=%s  body systems=%s"
         % (f"{a:,}", f"{b:,}", f"{c:,}"))


def load_coverage(con, cur):
    for src, table, cols in (
        ("medicare_coverage_icd10", "coverage_icd10",
         ["article_id", "article_version", "icd10_code_id", "icd10_code_version",
          "icd10_covered_group", "range", "sort_order", "description",
          "last_updated", "asterisk"]),
        ("medicare_coverage_hcpc", "coverage_hcpc",
         ["article_id", "article_version", "hcpc_code_id", "hcpc_code_version",
          "hcpc_code_group", "long_description", "short_description", "range",
          "last_updated"]),
    ):
        path = find(src)
        cur.execute(f"DROP TABLE IF EXISTS {table}")
        extra = ", code_dotted text" if table == "coverage_icd10" else ""
        cur.execute(f"CREATE TABLE {table} ("
                    + ", ".join(f"{c} text" for c in cols) + extra + ")")
        buf = io.StringIO(); w = csv.writer(buf)
        n = 0
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            rdr = csv.DictReader(fh)
            for row in rdr:
                vals = [row.get(c, "") or "" for c in cols]
                if table == "coverage_icd10":
                    vals.append(dot(row.get("icd10_code_id", "")))
                w.writerow(vals)
                n += 1
        buf.seek(0)
        cur.copy_expert(f"COPY {table} FROM STDIN WITH (FORMAT csv)", buf)
        cur.execute(f"CREATE INDEX {table}_art ON {table} (article_id)")
        if table == "coverage_icd10":
            cur.execute("CREATE INDEX cov_icd_dotted ON coverage_icd10 (code_dotted)")
        else:
            cur.execute("CREATE INDEX cov_hcpc_code ON coverage_hcpc (hcpc_code_id)")
        cur.execute(f"ANALYZE {table}")
        con.commit()
        emit("%s=%s rows" % (table, f"{n:,}"))

    cur.execute("""SELECT count(DISTINCT i.article_id)
                   FROM coverage_icd10 i
                   WHERE EXISTS (SELECT 1 FROM coverage_hcpc h
                                 WHERE h.article_id = i.article_id)""")
    emit("articles bridging procedure<->diagnosis: %s" % f"{cur.fetchone()[0]:,}")


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("EXECUTION STEP 4 -- ICD-side vocabularies")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()
    con = connect(); cur = con.cursor()
    cur.execute("SET statement_timeout='1800s'")
    load_icd10cm(con, cur)
    emit()
    load_ccsr(con, cur)
    emit()
    load_coverage(con, cur)

    emit()
    emit("=== JOIN PROOF: does the dotted normalisation actually bridge them? ===")
    cur.execute("""SELECT count(DISTINCT c.code_dotted) FROM coverage_icd10 c
                   JOIN icd10cm i ON i.code_dotted = c.code_dotted""")
    j = cur.fetchone()[0]
    cur.execute("SELECT count(DISTINCT code_dotted) FROM coverage_icd10")
    t = cur.fetchone()[0]
    emit("  coverage codes matching icd10cm on the dotted form: %s of %s (%.1f%%)"
         % (f"{j:,}", f"{t:,}", 100.0 * j / t))
    cur.execute("""SELECT count(DISTINCT x.code_dotted) FROM ccsr x
                   JOIN icd10cm i ON i.code_dotted = x.code_dotted""")
    j2 = cur.fetchone()[0]
    cur.execute("SELECT count(DISTINCT code_dotted) FROM ccsr")
    t2 = cur.fetchone()[0]
    emit("  CCSR codes matching icd10cm on the dotted form:     %s of %s (%.1f%%)"
         % (f"{j2:,}", f"{t2:,}", 100.0 * j2 / t2))
    emit("  (without normalisation this join returns nothing: coverage is "
         "dotted, the other two are dotless)")
    con.close()
    emit()
    emit("written to " + OUT)


main()
