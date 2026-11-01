"""Execution step 5 -- load trial CONDITIONS, MeSH descriptors and status.

ct_intervention carries only intervention names and aliases, which served the
molecule bridge. T3 needs the other half of a trial record: what the trial is
STUDYING, and the controlled-vocabulary descriptor the registry has already
attached to it.

Three tables, field paths verified against the archive:

  ct_study      protocolSection.identificationModule.nctId
                protocolSection.statusModule.overallStatus
                  -- the gate: a WITHDRAWN trial asserts nothing
  ct_condition  protocolSection.conditionsModule.conditions[]
                  -- sponsor-authored prose
  ct_mesh       derivedSection.conditionBrowseModule.meshes[].{id, term}
                derivedSection.conditionBrowseModule.ancestors[].{id, term}
                  -- the registry's OWN controlled vocabulary. This is rung N1:
                     an indication that arrives already normalised.

Source: the cached archive at C:\\DataExperiments\\Sources\\clinicaltrials.
"""
import sys
import csv
import io
import json
import os
import time
import zipfile

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\exec_05_trials_result.txt"
ZIP = r"C:\DataExperiments\Sources\clinicaltrials\ctg-studies.json.zip"


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
    emit("EXECUTION STEP 5 -- trial conditions, MeSH descriptors, status")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()
    con = connect(); cur = con.cursor()
    cur.execute("SET statement_timeout='3600s'")
    for t, ddl in (
        ("ct_study", "nct_id text, overall_status text, phase text, "
                     "start_date text, completion_date text"),
        ("ct_condition", "nct_id text, condition_text text"),
        ("ct_mesh", "nct_id text, mesh_id text, mesh_term text, "
                    "is_ancestor boolean"),
    ):
        cur.execute(f"DROP TABLE IF EXISTS {t}")
        cur.execute(f"CREATE TABLE {t} (load_seq bigserial PRIMARY KEY, {ddl})")
    con.commit()
    emit("created ct_study, ct_condition, ct_mesh")

    sb, cb, mb = io.StringIO(), io.StringIO(), io.StringIO()
    sw, cw, mw = csv.writer(sb), csv.writer(cb), csv.writer(mb)
    studies = conds = meshes = 0
    t0 = time.time()

    def flush():
        for buf, table, cols in (
            (sb, "ct_study", "nct_id,overall_status,phase,start_date,completion_date"),
            (cb, "ct_condition", "nct_id,condition_text"),
            (mb, "ct_mesh", "nct_id,mesh_id,mesh_term,is_ancestor"),
        ):
            buf.seek(0)
            cur.copy_expert(f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv)",
                            buf)
        con.commit()

    with zipfile.ZipFile(ZIP) as zf:
        for member in zf.namelist():
            if not member.lower().endswith(".json"):
                continue
            with zf.open(member) as fh:
                try:
                    doc = json.loads(fh.read())
                except Exception:
                    continue
            for d in (doc if isinstance(doc, list) else [doc]):
                ps = d.get("protocolSection") or {}
                ds = d.get("derivedSection") or {}
                nct = ((ps.get("identificationModule") or {}).get("nctId") or "")
                if not nct:
                    continue
                studies += 1
                st = (ps.get("statusModule") or {})
                dz = (ps.get("designModule") or {})
                phases = (dz.get("phases") or [])
                sw.writerow([nct, st.get("overallStatus") or "",
                             "|".join(phases),
                             (st.get("startDateStruct") or {}).get("date") or "",
                             (st.get("completionDateStruct") or {}).get("date") or ""])
                for c in ((ps.get("conditionsModule") or {}).get("conditions") or []):
                    c = (c or "").strip()
                    if c:
                        cw.writerow([nct, c]); conds += 1
                cbm = ds.get("conditionBrowseModule") or {}
                for key, anc in (("meshes", "false"), ("ancestors", "true")):
                    for m in (cbm.get(key) or []):
                        mid = (m.get("id") or "").strip()
                        trm = (m.get("term") or "").strip()
                        if mid or trm:
                            mw.writerow([nct, mid, trm, anc]); meshes += 1
                if studies % 50_000 == 0:
                    flush()
                    sb, cb, mb = io.StringIO(), io.StringIO(), io.StringIO()
                    sw, cw, mw = csv.writer(sb), csv.writer(cb), csv.writer(mb)
                    emit("  studies=%s conditions=%s mesh=%s  %.0fs"
                         % (f"{studies:,}", f"{conds:,}", f"{meshes:,}",
                            time.time() - t0))
    flush()
    emit("loaded studies=%s conditions=%s mesh_rows=%s"
         % (f"{studies:,}", f"{conds:,}", f"{meshes:,}"))

    for ddl in (
        "CREATE INDEX cs_nct ON ct_study (nct_id)",
        "CREATE INDEX cs_status ON ct_study (overall_status)",
        "CREATE INDEX cc_nct ON ct_condition (nct_id)",
        "CREATE INDEX cc_lower ON ct_condition (lower(condition_text))",
        "CREATE INDEX cm_nct ON ct_mesh (nct_id)",
        "CREATE INDEX cm_id ON ct_mesh (mesh_id)",
        "CREATE INDEX cm_term_lower ON ct_mesh (lower(mesh_term))",
        "CREATE INDEX cm_anc ON ct_mesh (is_ancestor)",
    ):
        cur.execute(ddl)
        con.commit()
    emit("indexed")

    emit()
    emit("=== WHAT WE NOW HAVE FOR RUNG N1 ===")
    cur.execute("SELECT count(DISTINCT nct_id) FROM ct_condition")
    a = cur.fetchone()[0]
    cur.execute("SELECT count(DISTINCT nct_id) FROM ct_mesh WHERE NOT is_ancestor")
    b = cur.fetchone()[0]
    cur.execute("SELECT count(DISTINCT mesh_id) FROM ct_mesh WHERE NOT is_ancestor")
    c = cur.fetchone()[0]
    cur.execute("SELECT count(DISTINCT lower(condition_text)) FROM ct_condition")
    d = cur.fetchone()[0]
    emit("  studies with a condition      : %s" % f"{a:,}")
    emit("  studies with a MeSH descriptor: %s" % f"{b:,}")
    emit("  distinct MeSH descriptors     : %s" % f"{c:,}")
    emit("  distinct condition strings    : %s" % f"{d:,}")
    emit("  -> MeSH collapses %s raw condition strings onto %s descriptors, "
         "which is why N1 outranks the prose rungs" % (f"{d:,}", f"{c:,}"))

    emit()
    emit("=== TRIAL STATUS, the N-rung gate ===")
    cur.execute("""SELECT overall_status, count(*) FROM ct_study
                   GROUP BY 1 ORDER BY 2 DESC LIMIT 12""")
    for s, n in cur.fetchall():
        emit("  %-28s %s" % (s or "(blank)", f"{n:,}"))
    con.close()
    emit()
    emit("written to " + OUT)


main()
