"""Crosswalk the 76 molecules the folded match recovered.

2,847,156 claims -- atorvastatin, hydrocodone/acetaminophen, azithromycin,
losartan/HCTZ among them -- had FDA labels all along and were never coded.

Same accepted artifacts as before, appending to llm_crosswalk rather than
rebuilding it: FDADrugEnrichmentTool, PROMPT 2 verbatim, gpt-5.6-luna, the
12,014-code catalog, batch 25, retest-on-drop mandatory.
"""
import os
import sys
import time

import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, r"C:\CHATHealthyLLC")
sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()
load_dotenv(r"C:\CHATHealthyLLC\.env")

from pipeline.MedicareProviderEvaluationDataPipeline.fda_drug_enrichment_tool import (  # noqa: E402
    FDADrugEnrichmentTool)

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\exec_14_recover_result.txt"
CATALOG = os.path.join(
    r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline",
    "data", "icd10_categories_top3levels.xlsx")
BATCH = 25
MAX_PROSE = 12000


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    con = psycopg2.connect(host="localhost", port=5432, user="postgres",
                           password=pw, dbname="medicare_join")
    con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='7200s'")

    emit("EXECUTION 14 -- crosswalk the molecules the folded match recovered")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("model=%s" % os.environ.get("CH_FDA_ENRICHMENT_MODEL"))
    emit()

    cur.execute("""
        SELECT ml.molecule_id,
               min(c.gnrc_name) AS a_name,
               left(string_agg(DISTINCT nullif(l.indications_and_usage,''),
                               E'\\n'), %s) AS prose
        FROM mol_label ml
        JOIN (SELECT DISTINCT set_id, indications_and_usage FROM fda_label) l
          ON l.set_id = ml.set_id
        LEFT JOIN cms_drug_resolution c ON c.molecule_id = ml.molecule_id
        WHERE NOT EXISTS (SELECT 1 FROM llm_crosswalk x
                          WHERE x.molecule_id = ml.molecule_id)
        GROUP BY 1
        HAVING left(string_agg(DISTINCT nullif(l.indications_and_usage,''),
                               E'\\n'), %s) <> ''
        ORDER BY 1
    """, (MAX_PROSE, MAX_PROSE))
    rows = cur.fetchall()
    records = [{"id": m, "set_id": m, "molecule": (nm or m),
                "indication_text": p}
               for m, nm, p in rows if (p or "").strip()]
    emit("molecules to crosswalk: %s" % f"{len(records):,}")
    if not records:
        emit("nothing to do")
        return

    tool = FDADrugEnrichmentTool(catalog=CATALOG, batch_size=BATCH,
                                 max_transient_retries=5, backoff_base_s=2.0)
    emit("catalog codes: %s" % f"{len(tool.catalog_codes):,}")

    persisted = {"n": 0}

    def sink(record, attempt):
        doc = record.model_dump()
        mol = str(doc.get("set_id") or "")
        for ind in (doc.get("indications") or []):
            codes = ind.get("codes") or []
            if not codes:
                cur.execute("""INSERT INTO llm_crosswalk VALUES
                               (%s,%s,%s,NULL,NULL,NULL,NULL,%s,%s)""",
                            (mol, ind.get("indication"),
                             ind.get("confidence_rationale"),
                             doc.get("usage_summary"), attempt))
                persisted["n"] += 1
            for c in codes:
                cur.execute("""INSERT INTO llm_crosswalk VALUES
                               (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (mol, ind.get("indication"),
                             ind.get("confidence_rationale"),
                             c.get("icd10_code"), c.get("level"),
                             c.get("confidence"), c.get("description"),
                             doc.get("usage_summary"), attempt))
                persisted["n"] += 1

    def run_pass(batch, attempt):
        t0 = time.time()
        got = tool.enrich(batch, sink=lambda r: sink(r, attempt),
                          on_batch_error=lambda k, e: emit(
                              "  batch error: %s" % str(e)[:140]),
                          on_batch_usage=lambda m: None)
        emit("  pass %d: fired=%s returned=%s  %.0fs"
             % (attempt, len(batch), len(got), time.time() - t0))
        return {str(r.set_id) for r in got}

    wanted = {r["set_id"] for r in records}
    returned = run_pass(records, 1)
    dropped = sorted(wanted - returned)
    emit()
    emit("=== RETEST-ON-DROP (mandatory) ===")
    emit("  fired=%s returned=%s dropped=%s"
         % (len(wanted), len(returned), len(dropped)))
    if dropped:
        retry = [r for r in records if r["set_id"] in set(dropped)]
        returned |= run_pass(retry, 2)
        emit("  still missing: %s" % len(sorted(wanted - returned)))

    emit()
    emit("rows written to llm_crosswalk: %s" % f"{persisted['n']:,}")
    cur.execute("SELECT count(DISTINCT molecule_id) FROM llm_crosswalk")
    emit("molecules now crosswalked in total: %s" % f"{cur.fetchone()[0]:,}")
    con.close()
    emit()
    emit("written to " + OUT)


main()
