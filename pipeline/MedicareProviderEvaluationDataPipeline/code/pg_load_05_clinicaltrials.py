"""Stage 5 -- load the ClinicalTrials.gov corpus: studies, interventions, conditions.

One pass over the archive, three tables, because three different questions are
asked of it and each wants its own grain:

  ct_study         one row per trial  -- its overall status and its phases
  ct_intervention  one row per (trial, intervention, alias)
  ct_condition     one row per (trial, condition)

phases arrives from the registry as a LIST and is stored as the registry's own
pipe-joined spelling rather than flattened to a single value: a phase 1/2 trial
carries PHASE1 and PHASE2 and claiming either alone would state a phase the
registry did not.

No records are chosen and none are capped. Every study in the archive is read.
A study whose JSON will not parse is counted and skipped, so a malformed record
is visible in the count rather than silently absent.

Source is the local archive that fetch_all_sources staged. clinicaltrials is a
direct-to-disk source, not one of the Azure-archived prefixes.
"""
import csv
import io
import json
import os
import sys
import time
import zipfile

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC")
sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
DB = "medicare_join"
SOURCES = r"C:\DataExperiments\Sources"
ZIP_PATH = os.path.join(SOURCES, "clinicaltrials", "ctg-studies.json.zip")
LOG = (r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline"
       r"\code\pg_load_05_result.txt")
FLUSH_EVERY = 50_000


def emit(msg=""):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    con = psycopg2.connect(host="localhost", port=5432, user="postgres",
                           password=pw, dbname=DB)
    con.autocommit = False
    return con


def main():
    if os.path.exists(LOG):
        os.remove(LOG)
    emit("STAGE 5 -- the ClinicalTrials.gov corpus")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    if not os.path.isfile(ZIP_PATH):
        raise ChatHealthyException(
            mode="config_error",
            component="pg_load_05_clinicaltrials",
            message=f"clinical-trials archive not found at {ZIP_PATH!r}; "
                    "run fetch_all_sources.py with 'clinicaltrials' first",
        )
    emit("archive %s (%.0f MB)" % (ZIP_PATH, os.path.getsize(ZIP_PATH) / 1e6))

    con = connect()
    cur = con.cursor()
    for name, ddl in (
        ("ct_study", """CREATE TABLE ct_study (
                          load_seq       bigserial PRIMARY KEY,
                          nct_id         text,
                          overall_status text,
                          phases         text)"""),
        ("ct_intervention", """CREATE TABLE ct_intervention (
                          load_seq   bigserial PRIMARY KEY,
                          nct_id     text,
                          ivtype     text,
                          name       text,
                          other_name text)"""),
        ("ct_condition", """CREATE TABLE ct_condition (
                          load_seq  bigserial PRIMARY KEY,
                          nct_id    text,
                          condition text)"""),
    ):
        cur.execute('DROP TABLE IF EXISTS "%s" CASCADE' % name)
        cur.execute(ddl)
    con.commit()
    emit("created ct_study, ct_intervention, ct_condition")

    sbuf, ibuf, cbuf = io.StringIO(), io.StringIO(), io.StringIO()
    sw, iw, cw = csv.writer(sbuf), csv.writer(ibuf), csv.writer(cbuf)
    studies = ivrows = condrows = unparsable = 0
    t0 = time.time()

    def flush():
        nonlocal sbuf, ibuf, cbuf, sw, iw, cw
        for buf, table, cols in ((sbuf, "ct_study",
                                  "nct_id,overall_status,phases"),
                                 (ibuf, "ct_intervention",
                                  "nct_id,ivtype,name,other_name"),
                                 (cbuf, "ct_condition", "nct_id,condition")):
            buf.seek(0)
            cur.copy_expert("COPY %s (%s) FROM STDIN WITH (FORMAT csv)"
                            % (table, cols), buf)
        con.commit()
        sbuf, ibuf, cbuf = io.StringIO(), io.StringIO(), io.StringIO()
        sw, iw, cw = csv.writer(sbuf), csv.writer(ibuf), csv.writer(cbuf)

    with zipfile.ZipFile(ZIP_PATH) as zf:
        for member in zf.namelist():
            if not member.lower().endswith(".json"):
                continue
            with zf.open(member) as fh:
                raw = fh.read()
            try:
                doc = json.loads(raw)
            except ValueError:
                unparsable += 1
                continue
            for d in (doc if isinstance(doc, list) else [doc]):
                studies += 1
                ps = d.get("protocolSection") or {}
                nct = ((ps.get("identificationModule") or {})
                       .get("nctId") or "").strip()
                status = ((ps.get("statusModule") or {})
                          .get("overallStatus") or "").strip()
                phases = (ps.get("designModule") or {}).get("phases") or []
                sw.writerow([nct, status, "|".join(phases)])

                for iv in ((ps.get("armsInterventionsModule") or {})
                           .get("interventions") or []):
                    nm = (iv.get("name") or "").strip()
                    tp = (iv.get("type") or "").strip()
                    for on in (iv.get("otherNames") or [None]):
                        iw.writerow([nct, tp, nm, (on or "").strip()])
                        ivrows += 1

                for c in ((ps.get("conditionsModule") or {})
                          .get("conditions") or []):
                    c = (c or "").strip()
                    if c:
                        cw.writerow([nct, c])
                        condrows += 1

                if studies % FLUSH_EVERY == 0:
                    flush()
                    emit("   studies=%s interventions=%s conditions=%s  %.0fs"
                         % (f"{studies:,}", f"{ivrows:,}", f"{condrows:,}",
                            time.time() - t0))
    flush()

    cur.execute("CREATE INDEX ct_study_nct ON ct_study (nct_id)")
    cur.execute("CREATE INDEX ct_iv_nct ON ct_intervention (nct_id)")
    cur.execute("CREATE INDEX ct_iv_name ON ct_intervention (upper(name))")
    cur.execute("CREATE INDEX ct_iv_other ON ct_intervention (upper(other_name))")
    cur.execute("CREATE INDEX ct_cond_nct ON ct_condition (nct_id)")
    for t in ("ct_study", "ct_intervention", "ct_condition"):
        cur.execute('ANALYZE "%s"' % t)
    con.commit()

    emit()
    emit("loaded in %.0f minutes" % ((time.time() - t0) / 60))
    if unparsable:
        emit("   archive members that would not parse: %s" % f"{unparsable:,}")
    for t in ("ct_study", "ct_intervention", "ct_condition"):
        cur.execute('SELECT count(*) FROM "%s"' % t)
        emit("   %-18s %12s rows" % (t, f"{cur.fetchone()[0]:,}"))
    cur.execute("SELECT count(DISTINCT nct_id) FROM ct_study")
    emit("   distinct trials    %12s" % f"{cur.fetchone()[0]:,}")
    cur.execute("""SELECT overall_status, count(*) FROM ct_study
                   GROUP BY 1 ORDER BY 2 DESC""")
    emit("   overall_status as the registry publishes it:")
    for s, n in cur.fetchall():
        emit("      %-28s %s" % (s or "(absent)", f"{n:,}"))
    cur.execute("""SELECT phases, count(*) FROM ct_study
                   GROUP BY 1 ORDER BY 2 DESC""")
    emit("   phases as the registry publishes it:")
    for p, n in cur.fetchall():
        emit("      %-28s %s" % (p or "(absent)", f"{n:,}"))
    con.close()
    emit()
    emit("written to " + LOG)


main()
