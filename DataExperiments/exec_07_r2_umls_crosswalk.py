"""Execution step 7 -- close R2: MeSH descriptor -> ICD-10-CM via UMLS.

The design named R2 as a joined_coded_source route and recorded it as the one
named gap, because MRCONSO was not a declared source. It does not need to be:
UTS exposes a crosswalk endpoint that answers the same question per descriptor,
import sys
from the same authority, without a 5 GB release download.

    /rest/crosswalk/current/source/MSH/{descriptor}?targetSource=ICD10CM

Verified before use:
    D003924 Diabetes Mellitus, Type 2 -> E11
    D006333 Heart Failure             -> I50, I50.9, I50.1, I50.81, I50.810

It returns BOTH levels, which is what the chain needs.

Only the descriptors the chain actually uses are fetched. Results are cached on
disk so a rerun costs nothing and the measurement is reproducible without
re-hitting a licensed service.
"""
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

load_dotenv(r"C:\CHATHealthyLLC\.env")

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\exec_07_r2_result.txt"
CACHE = r"C:\DataExperiments\Sources\umls_crosswalk_cache.json"
KEY = os.environ.get("UMLS_API_KEY", "").strip()
WORKERS = 5


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def fetch(descriptor):
    """-> list of (icd10_code, name). [] means UMLS has no mapping."""
    url = ("https://uts-ws.nlm.nih.gov/rest/crosswalk/current/source/MSH/"
           f"{urllib.parse.quote(descriptor)}?targetSource=ICD10CM"
           f"&apiKey={urllib.parse.quote(KEY)}")
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                body = json.loads(r.read())
            return [(x.get("ui", ""), x.get("name", ""))
                    for x in (body.get("result") or []) if x.get("ui")]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []                      # no mapping is an answer
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return None                        # unknown: do not cache
        except Exception:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return None
    return None


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("EXECUTION STEP 7 -- R2 MeSH -> ICD-10-CM via UMLS crosswalk")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()
    if not KEY:
        raise ChatHealthyException("runtime_error", "UMLS_API_KEY absent")

    con = connect(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='1800s'")

    cur.execute("""SELECT DISTINCT replace(indication_id,'mesh:','')
                   FROM molecule_indication
                   WHERE indication_id LIKE 'mesh:%'""")
    wanted = sorted({r[0] for r in cur.fetchall() if r[0]})
    emit("descriptors the chain uses: %s" % f"{len(wanted):,}")

    cache = {}
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as fh:
            cache = json.load(fh)
        emit("cache on disk: %s descriptors" % f"{len(cache):,}")
    todo = [d for d in wanted if d not in cache]
    emit("to fetch: %s" % f"{len(todo):,}")

    if todo:
        q = queue.Queue()
        for d in todo:
            q.put(d)
        lock = threading.Lock()
        done = [0]
        failed = []
        t0 = time.time()

        def worker():
            while True:
                try:
                    d = q.get_nowait()
                except queue.Empty:
                    return
                res = fetch(d)
                with lock:
                    if res is None:
                        failed.append(d)
                    else:
                        cache[d] = res
                    done[0] += 1
                    if done[0] % 200 == 0:
                        emit("   fetched %s/%s  %.0fs  failures=%s"
                             % (f"{done[0]:,}", f"{len(todo):,}",
                                time.time() - t0, len(failed)))
                q.task_done()

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        emit("fetched %s descriptors in %.0fs; unrecoverable failures=%s"
             % (f"{done[0]:,}", time.time() - t0, len(failed)))
        if failed:
            emit("  failed (not cached, will retry on rerun): %s"
                 % ", ".join(failed[:10]))
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        emit("cache written: %s" % CACHE)

    # ---- load into a table -------------------------------------------------
    cur.execute("DROP TABLE IF EXISTS mesh_icd_umls")
    cur.execute("""CREATE TABLE mesh_icd_umls (
                     mesh_id text, icd10_code text, icd_name text)""")
    import csv as _csv
    import io as _io
    buf = _io.StringIO(); w = _csv.writer(buf)
    mapped = unmapped = rows = 0
    for d in wanted:
        res = cache.get(d)
        if res is None:
            continue
        if res:
            mapped += 1
            for code, name in res:
                w.writerow([d, code, name]); rows += 1
        else:
            unmapped += 1
    buf.seek(0)
    cur.copy_expert("COPY mesh_icd_umls FROM STDIN WITH (FORMAT csv)", buf)
    cur.execute("CREATE INDEX miu_mesh ON mesh_icd_umls (mesh_id)")
    cur.execute("CREATE INDEX miu_code ON mesh_icd_umls (icd10_code)")
    cur.execute("ANALYZE mesh_icd_umls")

    emit()
    emit("=== R2 COVERAGE OF THE DESCRIPTORS ===")
    emit("  descriptors WITH an ICD-10-CM mapping : %s" % f"{mapped:,}")
    emit("  descriptors with NO mapping in UMLS   : %s" % f"{unmapped:,}")
    emit("  mapping rows                          : %s" % f"{rows:,}")
    if mapped + unmapped:
        emit("  share of descriptors mapped           : %.1f%%"
             % (100.0 * mapped / (mapped + unmapped)))
    cur.execute("""SELECT count(*) FILTER (WHERE length(replace(icd10_code,'.',''))<=3),
                          count(*) FILTER (WHERE length(replace(icd10_code,'.',''))>3)
                   FROM mesh_icd_umls""")
    l3, l4 = cur.fetchone()
    emit("  3-character codes returned=%s   4+ character=%s"
         % (f"{l3:,}", f"{l4:,}"))
    cur.execute("""SELECT count(DISTINCT m.icd10_code) FROM mesh_icd_umls m
                   JOIN icd10cm i ON i.code_dotted = m.icd10_code""")
    j = cur.fetchone()[0]
    cur.execute("SELECT count(DISTINCT icd10_code) FROM mesh_icd_umls")
    t = cur.fetchone()[0]
    emit("  codes recognised by the loaded ICD-10-CM set: %s of %s (%.1f%%)"
         % (f"{j:,}", f"{t:,}", 100.0 * j / max(t, 1)))

    emit()
    emit("=== WORKED EXAMPLES ===")
    for probe in ("D006333", "D003924", "D012559", "D009369"):
        cur.execute("""SELECT m.icd10_code, m.icd_name, i.is_billable
                       FROM mesh_icd_umls m
                       LEFT JOIN icd10cm i ON i.code_dotted = m.icd10_code
                       WHERE m.mesh_id=%s ORDER BY 1 LIMIT 5""", (probe,))
        got = cur.fetchall()
        cur.execute("""SELECT DISTINCT mesh_term FROM ct_mesh
                       WHERE mesh_id=%s LIMIT 1""", (probe,))
        nm = cur.fetchone()
        emit("  %s %s" % (probe, ("(%s)" % nm[0]) if nm else ""))
        for code, name, bill in got:
            emit("     %-10s L%s  %s" % (code, 4 if bill else 3, str(name)[:50]))
        if not got:
            emit("     no mapping")
    con.close()
    emit()
    emit("written to " + OUT)


main()
