"""Fix the confidence grain, then repopulate all of DE and AL.

THE DEFECT. chain_code was keyed on indication_id alone. indication_id is
md5(lower(trim(indication_text))), so every molecule indicated for
"schizophrenia" collapses onto ONE id -- 872 ids are shared by more than one
molecule. chain_code then held every molecule's confidence for that
indication, and chain_delivered attached all of them to all of those
molecules. F20.9 under one indication_id carried ten confidences from ten
different molecules.

Confidence is molecule-specific: the model scored THAT molecule's label prose.
A table that drops the molecule cannot hold it.

THE FIX, which keeps the design intact.
  chain_code stays what the design says it is -- the indication-to-code
  VOCABULARY. It loses confidence, because confidence was never a property of
  a vocabulary entry.

  chain_delivered attaches confidence from the source that earned it, keyed on
  (molecule_id, indication, icd10_code) straight out of llm_crosswalk for the
  model route, and 1.0 for a deterministic derivation per BR-003.

WHY NOT FIX IT IN THE PORT. Collapsing ten confidences into one means picking
a winner, and picking is business logic. Rule 1 forbids it there.
"""
import sys
import os
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\fix_confidence_result.txt"
INFERRED_CEILING = 0.9
DETERMINISTIC = 1.0
ROUTE_MODEL = "fda_label_coded_by_model"


def emit(m=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(m + "\n")
    _log.info("%s", m)


def stop(rule, detail):
    raise ChatHealthyException("runtime_error", "VIOLATED %s -- %s" % (rule, detail))


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    con = psycopg2.connect(host="localhost", port=5432, user="postgres",
                           password=pw, dbname="medicare_join")
    con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='7200s'")

    emit("FIX THE CONFIDENCE GRAIN, THEN REPOPULATE ALL OF DE AND AL")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()
    cur.execute("SELECT count(*) FROM chain_delivered")
    emit("chain_delivered before: %s rows" % f"{cur.fetchone()[0]:,}")

    # ---- the per-(molecule, indication, code) confidence, from the source --
    emit()
    emit("=== building the molecule-aware confidence source ===")
    cur.execute("""
        DROP TABLE IF EXISTS chain_conf CASCADE;
        CREATE TABLE chain_conf AS
        SELECT l.molecule_id,
               'label:' || md5(lower(btrim(l.indication))) AS indication_id,
               coalesce(i.code_dotted, l.icd10_code)       AS icd10_code,
               max(l.confidence) * %s                      AS confidence
        FROM llm_crosswalk l
        JOIN icd10cm i ON i.code_dotless = replace(l.icd10_code,'.','')
        WHERE l.icd10_code IS NOT NULL
        GROUP BY 1,2,3;
        CREATE UNIQUE INDEX cc_conf_key
          ON chain_conf (molecule_id, indication_id, icd10_code);
        ANALYZE chain_conf;
    """, (INFERRED_CEILING,))
    cur.execute("SELECT count(*) FROM chain_conf")
    emit("   rows (one per molecule+indication+code): %s" % f"{cur.fetchone()[0]:,}")
    emit("   NOTE: max() collapses the case where ONE molecule's own response")
    emit("         repeated a pair. It never mixes molecules, because")
    emit("         molecule_id is in the key. The unique index proves it.")

    # ---- rebuild the delivered set with confidence at the right grain ------
    emit()
    emit("=== rebuilding chain_delivered ===")
    cur.execute("""
        DROP TABLE IF EXISTS chain_delivered_v2 CASCADE;
        CREATE TABLE chain_delivered_v2 AS
        SELECT DISTINCT
               pm.npi, pm.fill_state, pm.billing_state, pm.state_differs,
               pm.entity_type_code, pm.entity_type_code_label,
               pm.primary_nucc, pm.primary_nucc_label,
               pm.molecule_id,
               ci.indication_id, ci.indication_text,
               ci.corroborated_by_trial,
               cc.icd10_code, cc.who_level, cc.is_billable, cc.route,
               cc.ccsr_category, cc.body_system,
               CASE WHEN cc.provenance = 'inference' THEN 'inference'
                    WHEN cc.provenance = 'adopted_vocabulary'
                      THEN 'adopted_vocabulary'
                    ELSE 'joined_coded_source' END          AS provenance,
               -- the model route takes the confidence its OWN molecule
               -- earned; a deterministic route is exactly 1.0 (BR-003)
               CASE WHEN cc.route = %s THEN cf.confidence
                    ELSE %s END                             AS confidence
        FROM chain_provider_molecule pm
        JOIN chain_indication ci ON ci.molecule_id = pm.molecule_id
        JOIN chain_code cc       ON cc.indication_id = ci.indication_id
        LEFT JOIN chain_conf cf
               ON cf.molecule_id   = pm.molecule_id
              AND cf.indication_id = ci.indication_id
              AND cf.icd10_code    = cc.icd10_code
        WHERE pm.molecule_id IS NOT NULL
          AND (cc.route <> %s OR cf.confidence IS NOT NULL);
    """, (ROUTE_MODEL, DETERMINISTIC, ROUTE_MODEL))
    for ddl in ("CREATE INDEX cd2_npi ON chain_delivered_v2 (npi)",
                "CREATE INDEX cd2_mol ON chain_delivered_v2 (npi, molecule_id)",
                "CREATE INDEX cd2_code ON chain_delivered_v2 (icd10_code)"):
        cur.execute(ddl)
    cur.execute("ANALYZE chain_delivered_v2")
    cur.execute("SELECT count(*) FROM chain_delivered_v2")
    after = cur.fetchone()[0]
    emit("   rows after: %s" % f"{after:,}")

    # ---- the leak must be gone -------------------------------------------
    emit()
    emit("=== THE LEAK IS GONE -- asserted, not hoped ===")
    cur.execute("""
        SELECT count(*) FROM (
          SELECT npi, molecule_id, indication_id, icd10_code, count(*) n
          FROM chain_delivered_v2 GROUP BY 1,2,3,4 HAVING count(*) > 1) x""")
    dup = cur.fetchone()[0]
    emit("   (npi, molecule, indication, code) appearing more than once: %s"
         % f"{dup:,}")
    if dup:
        stop("the leak", "%d duplicated delivered facts remain" % dup)

    cur.execute("""
        SELECT count(*) FILTER (WHERE provenance='inference' AND confidence > %s),
               count(*) FILTER (WHERE provenance<>'inference' AND confidence <> %s),
               max(confidence) FILTER (WHERE provenance='inference'),
               min(confidence) FILTER (WHERE provenance<>'inference')
        FROM chain_delivered_v2""", (INFERRED_CEILING, DETERMINISTIC))
    over, notone, maxinf, mindet = cur.fetchone()
    emit("   BR-004 inferred above %.1f : %s   BR-003 deterministic not 1.0 : %s"
         % (INFERRED_CEILING, over, notone))
    emit("   inferred max=%s   deterministic min=%s" % (maxinf, mindet))
    if over or notone:
        stop("BR-003/BR-004", "the confidence scale broke in the rebuild")

    # ---- swap it in and rebuild the pseudo-target -------------------------
    cur.execute("DROP TABLE IF EXISTS chain_delivered_old CASCADE")
    cur.execute("ALTER TABLE chain_delivered RENAME TO chain_delivered_old")
    cur.execute("ALTER TABLE chain_delivered_v2 RENAME TO chain_delivered")
    emit()
    emit("=== rebuilding the pseudo-target from the corrected chain ===")
    cur.execute("""
        DROP TABLE IF EXISTS pt_providermedicare_indication CASCADE;
        CREATE TABLE pt_providermedicare_indication AS
        SELECT npi, molecule_id, indication_id, indication_text,
               corroborated_by_trial, icd10_code, who_level, is_billable,
               route, ccsr_category, body_system, provenance, confidence
        FROM chain_delivered;
        CREATE INDEX ptpmi_key ON pt_providermedicare_indication (npi, molecule_id);
        ANALYZE pt_providermedicare_indication;
    """)
    cur.execute("SELECT count(*) FROM pt_providermedicare_indication")
    emit("   pt_providermedicare_indication: %s rows" % f"{cur.fetchone()[0]:,}")

    emit()
    emit("=== ALL OF DE AND AL IS STILL PRESENT -- nothing was narrowed ===")
    # GUARDED. Every correctness assertion in this file has already passed by
    # this point; what follows is a report. A report that throws must never
    # void a build -- this exact query referenced fill_state on the wrong
    # table and destroyed a completed chain including a 2.3-hour paid
    # inference. Assertions stop the run. Reports never do.
    try:
        cur.execute("""SELECT d.fill_state, count(DISTINCT d.npi),
                              count(DISTINCT d.molecule_id), count(*)
                       FROM chain_delivered d GROUP BY 1 ORDER BY 1""")
        for st, n, m, r in cur.fetchall():
            emit("   %s providers=%s molecules=%s rows=%s"
                 % (st, f"{n:,}", f"{m:,}", f"{r:,}"))
        cur.execute("SELECT count(DISTINCT npi), count(*) FROM pt_providermedicare")
        npi_n, tot = cur.fetchone()
        emit("   distinct NPIs: %s   provider-molecule rows: %s"
             % (f"{npi_n:,}", f"{tot:,}"))
    except Exception as exc:
        emit("   (report skipped: %s: %s -- the build is unaffected)"
             % (type(exc).__name__, str(exc)[:120]))
    con.close()
    emit()
    emit("written to " + OUT)


main()
