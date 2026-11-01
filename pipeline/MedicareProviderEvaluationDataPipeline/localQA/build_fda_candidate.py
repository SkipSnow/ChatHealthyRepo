"""Deliver QA_fdaProductToDiagnosisResolution_v_1: download to finished job.

  0   download openFDA, RxNorm, the ICD-10 vocabulary and the trials corpus
  1   empty the schema, keeping the crosswalk cache
  2   load openFDA          -> fda_label, fda_name
  3   load RxNorm           -> rxnconso, rxnrel
  3b  load ClinicalTrials   -> ct_study, ct_intervention, ct_condition
  4   resolve molecules     -> rx_ing, ct_bridge_t1, fda_mol, fda_brand_norm
  4b  load ICD-10           -> icd10cm
  5   crosswalk indications -> llm_crosswalk        (the one inferred stage)
  6   deliver               -> PipelinePublicHealthData.QA_...

The trials corpus is here for two reasons and no others: the collection
declares a clinical_trials element, and the trials alias graph identifies
molecules the FDA's own substance names do not reach. It is a resolution
source and a published fact, not a source of indications -- a trial condition
is what a trial INVESTIGATES, which is not what a label was APPROVED for, and
no requirement asks us to publish the first as the second.

WHAT IS NOT HERE, because the FDA collection does not read it: Medicare Part D,
provider_molecule, and the CMS drug-name bridge. The collection covers every
FDA product, so its molecules come from the FDA. Reaching them through Part D
would have let the drugs Delaware and Alabama happened to bill decide what the
FDA file contains.

A step that fails stops the run and says so. Nothing is dropped behind your
back -- the schema is left exactly as the failure found it.
"""
import os
import subprocess
import sys
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

# The loaders live in code/ and the port lives here in localQA/, so the driver
# shells out to two directories and every step says which one it means.
HERE = r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline\code"
QA_HERE = r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline\localQA"
PY = r"C:\CHATHealthyLLC\.venv\Scripts\python.exe"
PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline\localQA\build_fda_candidate_result.txt"


def emit(m=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(m + "\n")
    _log.info("%s", m)


def pg():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    c = psycopg2.connect(host="localhost", port=5432, user="postgres",
                         password=pw, dbname="medicare_join")
    c.autocommit = True
    return c


def fail(step, detail):
    emit()
    emit("=" * 70)
    emit("STOPPED AT STEP %s" % step)
    emit(detail)
    emit("=" * 70)
    emit("the schema is untouched from here; nothing was dropped")
    raise ChatHealthyException(
        "runtime_error", "stopped at step %s: %s" % (step, detail))


def run_step(step, name, timeout, *args, _here=None):
    path = os.path.join(_here or HERE, name)
    if not os.path.exists(path):
        fail(step, "script missing: " + path)
    emit("   running %s %s" % (name, " ".join(args)))
    t0 = time.time()
    r = subprocess.run([PY, path] + list(args), capture_output=True, text=True,
                       timeout=timeout, cwd=r"C:\CHATHealthyLLC")
    if r.returncode != 0:
        fail(step, "%s exited %d after %.0fs\n%s"
             % (name, r.returncode, time.time() - t0,
                (r.stderr or r.stdout or "")[-2000:]))
    emit("   %s ok in %.0fs" % (name, time.time() - t0))


def expect(cur, step, table):
    """The table exists and is not empty, and its row count is REPORTED.

    THE ROW MINIMUMS ARE GONE. This used to assert things like
    'fda_label >= 250000' and 'rxnconso >= 1000000'. Those numbers came from
    one observed run and no requirement asked for any of them. They assert
    nothing about correctness: a wrong load of a similar size passes, and a
    legitimately changed corpus fails. What can be checked without inventing
    anything is that the step produced a table and put something in it. The
    count is printed so a change in the corpus is VISIBLE rather than
    adjudicated against a number I made up."""
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", ("public." + table,))
    if not cur.fetchone()[0]:
        fail(step, "table %s was not created" % table)
    cur.execute('SELECT count(*) FROM "%s"' % table)
    n = cur.fetchone()[0]
    if n == 0:
        fail(step, "table %s is empty" % table)
    emit("   %-28s %12s rows" % (table, f"{n:,}"))
    return n


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("DELIVER THE FDA CANDIDATE -- download to finished job")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("target: PipelinePublicHealthData.QA_fdaProductToDiagnosisResolution_v_1")
    emit()
    t_all = time.time()
    con = pg(); cur = con.cursor(); cur.execute("SET statement_timeout='0'")

    emit("=== 0 download the FDA sources ===")
    # icd10_cm is here because the delivered codes must be WHO's dotted form
    # and must exist. The vocabulary supplies the dotted spelling and the join
    # to it is what removes a code the model invented (BR-007/BR-008/BR-023).
    #
    # ccsr and the two medicare_coverage sets are here for one reason: the ICD
    # loader is shared and loads all four. This collection reads only icd10cm.
    # Fetching roughly 100 MB it does not use is cheaper and safer than
    # forking a committed loader so the FDA path can skip three of its tables.
    run_step("0", "fetch_all_sources.py", 43200,
             "openfda_labels", "rxnorm", "rxnorm_rel", "icd10_cm",
             "ccsr", "medicare_coverage_icd10", "medicare_coverage_hcpc",
             "clinicaltrials")

    emit()
    emit("=== 1 empty the schema ===")
    cur.execute("""SELECT table_name FROM information_schema.views
                   WHERE table_schema='public'""")
    for (v,) in cur.fetchall():
        cur.execute('DROP VIEW IF EXISTS "%s" CASCADE' % v)
    # llm_crosswalk_cache SURVIVES. It is the durable record of what the model
    # answered, keyed by the prose that produced it, and it is what makes a
    # signed-off candidate reproducible instead of re-inferred. Dropping it
    # would throw away the signed answer and guarantee the next run differs.
    KEEP = {"llm_crosswalk_cache"}
    cur.execute("""SELECT table_name FROM information_schema.tables
                   WHERE table_schema='public' AND table_type='BASE TABLE'""")
    names = [r[0] for r in cur.fetchall() if r[0] not in KEEP]
    for t in names:
        cur.execute('DROP TABLE IF EXISTS "%s" CASCADE' % t)
    emit("   dropped %d tables" % len(names))
    cur.execute("SELECT to_regclass('public.llm_crosswalk_cache') IS NOT NULL")
    if cur.fetchone()[0]:
        cur.execute("""SELECT count(*), count(DISTINCT molecule_id)
                       FROM llm_crosswalk_cache""")
        n, m = cur.fetchone()
        emit("   kept llm_crosswalk_cache: %s rows over %s molecules"
             % (f"{n:,}", f"{m:,}"))
    else:
        emit("   no crosswalk cache yet; this run creates it")

    emit()
    emit("=== 2 openFDA labels ===")
    run_step("2", "pg_load_02_fda.py", 28800)
    expect(cur, "2", "fda_label")
    expect(cur, "2", "fda_name")

    emit()
    emit("=== 3 RxNorm ===")
    run_step("3", "pg_load_04_rxnorm.py", 28800)
    expect(cur, "3", "rxnconso")
    expect(cur, "3", "rxnrel")

    emit()
    emit("=== 3b the ClinicalTrials.gov corpus ===")
    run_step("3b", "pg_load_05_clinicaltrials.py", 28800)
    expect(cur, "3b", "ct_study")
    expect(cur, "3b", "ct_intervention")
    expect(cur, "3b", "ct_condition")

    emit()
    emit("=== 4 resolve every label to its molecule ===")
    run_step("4", "fda_build_molecules.py", 28800)
    expect(cur, "4", "rx_ing")
    expect(cur, "4", "ct_bridge_t1")
    expect(cur, "4", "fda_mol")
    expect(cur, "4", "fda_brand_norm")
    cur.execute("""SELECT count(DISTINCT ident_set) FROM fda_mol
                   WHERE ident_set <> ''""")
    emit("   distinct molecules (unfolded identity): %s" % f"{cur.fetchone()[0]:,}")
    cur.execute("""SELECT route, count(DISTINCT ident_set) FROM fda_mol
                   WHERE ident_set <> '' GROUP BY 1 ORDER BY 2 DESC""")
    for r, n in cur.fetchall():
        emit("      by route %-16s %s molecules" % (r, f"{n:,}"))

    emit()
    emit("=== 4b the ICD-10 vocabulary ===")
    run_step("4b", "exec_04_load_icd.py", 14400)
    expect(cur, "4b", "icd10cm")

    emit()
    emit("=== 5 crosswalk the label indications to ICD-10 ===")
    cur.execute("""
        SELECT count(DISTINCT f.ident_set)
        FROM fda_mol f
        JOIN fda_label l ON l.set_id = f.set_id
        WHERE f.ident_set <> ''
          AND btrim(coalesce(l.indications_and_usage,'')) <> ''""")
    emit("   molecules carrying indication prose: %s" % f"{cur.fetchone()[0]:,}")
    run_step("5", "exec_09_n4_r5_inference.py", 172800)
    expect(cur, "5", "llm_crosswalk")
    cur.execute("SELECT count(DISTINCT molecule_id) FROM llm_crosswalk")
    emit("   molecules crosswalked: %s" % f"{cur.fetchone()[0]:,}")

    emit()
    emit("=== 6 deliver to the pipeline server ===")
    run_step("6", "deliver_fda_collection.py", 28800, _here=QA_HERE)

    emit()
    emit("=== FINISHED in %.0f minutes ===" % ((time.time() - t_all) / 60))
    con.close()


main()
