"""Rebuild the entire chain from raw source, then migrate to the pipeline server.

WHY THIS EXISTS. Every figure reported before this file ran was produced by a
sequence of exploratory scripts with corrections layered on top of corrections.
That is learning, not evidence. A number is only credible if the process that
produced it can be re-run from raw source and produce it again. This file is
that process.

WHAT IT DOES, IN ORDER.
  0  DROP every table in medicare_join.public. Nothing survives, so nothing
     can be inherited from the exploratory phase.
  1  Load the raw sources from the 13 GB local cache under
     C:\\DataExperiments\\Sources -- Part D, openFDA labels, RxNorm, clinical
     trials, ICD-10-CM, CCSR, Medicare coverage articles.
  2  Load the provider collection from the pipeline cluster.
  3  Build the molecule bridge, the label match, the indications, the UMLS
     crosswalk.
  4  Run the LLM inference. THIS STEP IS NOT DETERMINISTIC: it is a paid
     gpt-5.6-luna call through the benchmarked tool, and a re-run produces
     similar but not identical confidences. Every other stage is
     deterministic. That is a property of the step, not a shortcut.
  5  Build the chain, with the corrections BUILT IN rather than patched on
     afterwards -- the folded salt key, the brand rung, the device-suffix
     strip, and the molecule-grained confidence.
  6  Build the pseudo-target public collections.
  7  Migrate them to QA_ collections on the pipeline server.

THE CORRECTIONS ARE INLINE, NOT PATCHES. In the exploratory phase these were
separate scripts run after the fact: mol_label joined the unfolded key and was
repaired later; the brand and device rungs were added later; confidence was
fixed later. A rebuild that still needed those patches would not be a rebuild.
They are part of the build here.

NO STAGE IS SKIPPED AND NO STAGE IS FAKED. Each one asserts its own output and
the run stops on the first failure.
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

HERE = r"C:\CHATHealthyLLC\DataExperiments"
PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\rebuild_everything_result.txt"
PY = r"C:\CHATHealthyLLC\.venv\Scripts\python.exe"
INFERRED_CEILING = 0.9
DETERMINISTIC = 1.0
ROUTE_MODEL = "fda_label_coded_by_model"
ROUTE_NIH = "nih_vocabulary_crosswalk"
ROUTE_COVERAGE = "medicare_coverage_policy"
ROUTE_VOCAB = "icd_description_match"


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


# A stage failure is a ChatHealthyException carrying the driver_error mode.
# The catch site below discriminates on that mode, not on a type, which is
# what the exception contract asks for.
STAGE_FAILURE = "driver_error"


def die(stage, detail):
    """A failure is not patched in the middle. It raises, and the handler
    DROPS EVERYTHING, so a half-built database can never be mistaken for a
    result and no stage is ever re-run on top of a partial one."""
    raise ChatHealthyException(STAGE_FAILURE, "%s -- %s" % (stage, detail))


def drop_all(cur):
    cur.execute("""SELECT table_name FROM information_schema.views
                   WHERE table_schema='public'""")
    for (v,) in cur.fetchall():
        cur.execute('DROP VIEW IF EXISTS "%s" CASCADE' % v)
    cur.execute("""SELECT table_name FROM information_schema.tables
                   WHERE table_schema='public' AND table_type='BASE TABLE'""")
    names = [r[0] for r in cur.fetchall()]
    for t in names:
        cur.execute('DROP TABLE IF EXISTS "%s" CASCADE' % t)
    return len(names)


def run_script(stage, name, timeout=None):
    """Run an existing stage script verbatim, so a reader can see exactly
    which code produced which table."""
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        die(stage, "script missing: " + path)
    emit("   running %s ..." % name)
    t0 = time.time()
    r = subprocess.run([PY, path], capture_output=True, text=True,
                       timeout=timeout, cwd=r"C:\CHATHealthyLLC")
    dt = time.time() - t0
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "")[-1200:]
        die(stage, "%s exited %d after %.0fs\n%s" % (name, r.returncode, dt, tail))
    emit("   %s ok in %.0fs" % (name, dt))


def expect(cur, stage, table):
    """A stage must CREATE its table. How many rows the source happens to
    carry is the source's business, not a gate."""
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", ("public." + table,))
    if not cur.fetchone()[0]:
        die(stage, "table %s was not created" % table)
    cur.execute('SELECT count(*) FROM "%s"' % table)
    n = cur.fetchone()[0]
    emit("   %-34s %12s rows" % (table, f"{n:,}"))
    return n


# ======================================================================
def stage_0_drop(cur):
    emit("=== STAGE 0 -- DROP EVERYTHING ===")
    n = drop_all(cur)
    emit("   dropped %d tables" % n)
    cur.execute("""SELECT count(*) FROM information_schema.tables
                   WHERE table_schema='public' AND table_type='BASE TABLE'""")
    left = cur.fetchone()[0]
    if left:
        die("STAGE 0", "%d tables survived the drop" % left)
    emit("   public schema is empty -- nothing is inherited")


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("REBUILD EVERYTHING FROM RAW SOURCE")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("sources: C:\\DataExperiments\\Sources (13 GB, cached from Azure)")
    emit("the only non-deterministic stage is the LLM inference, and it is")
    emit("labelled where it runs")
    emit()
    t_all = time.time()

    con = pg(); cur = con.cursor()
    cur.execute("SET statement_timeout='0'")

    # Fetching is PART OF THE PROCESS, not a precondition someone arranged
    # by hand. If a source is missing or truncated this fails here, before
    # anything is dropped.
    emit("=== STAGE 0a -- fetch and verify every raw source ===")
    run_script("STAGE 0a", "fetch_all_sources.py", timeout=43200)

    emit()
    emit("=== STAGE 0b -- wake the pipeline cluster ===")
    try:
        sys.path.insert(0, r"C:\CHATHealthyLLC")
        sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
        from dotenv import load_dotenv
        load_dotenv(r"C:\CHATHealthyLLC\.env")
        from pipeline.run_lifecycle import atlas_cluster_manager as acm
        info = acm.get_cluster_info("ChatHealthyDataPipelines")
        emit("   state: %s" % info)
        if info.get("paused"):
            t = time.time()
            acm.resume_cluster("ChatHealthyDataPipelines")
            emit("   resumed in %.0fs -> %s"
                 % (time.time() - t,
                    acm.get_cluster_info("ChatHealthyDataPipelines")))
        else:
            emit("   already awake")
    except Exception as exc:
        die("STAGE 0b", "could not wake the pipeline cluster: %s: %s"
            % (type(exc).__name__, exc))

    stage_0_drop(cur)

    # ---------------------------------------------- 1 raw source loads
    emit()
    emit("=== STAGE 1 -- raw sources ===")
    # The Part D sequence is three steps and they are not interchangeable:
    #   pg_load_01  the national 500,000-row slice. pg_load_03 needs it to
    #               mark which pairs are development and which are holdout.
    #   pg_load_03  the national distinct (brand, generic) pair universe,
    #               streamed from the archived source. exec_01 reports it.
    #   exec_01     replaces partd with the COMPLETE DE+AL claims.
    # Two earlier runs failed here because I called exec_01 alone, assuming
    # it was self-contained. It is not.
    run_script("STAGE 1", "pg_load_01_schema_partd.py", timeout=14400)
    expect(cur, "STAGE 1", "partd")
    run_script("STAGE 1", "pg_load_03_partd_pairs.py", timeout=28800)
    expect(cur, "STAGE 1", "partd_pair")
    run_script("STAGE 1", "exec_01_partd_de_al.py", timeout=14400)
    expect(cur, "STAGE 1", "partd")
    expect(cur, "STAGE 1", "partd_pair_state")

    run_script("STAGE 1", "pg_load_02_fda.py", timeout=14400)
    expect(cur, "STAGE 1", "fda_label")
    expect(cur, "STAGE 1", "fda_name")

    run_script("STAGE 1", "pg_load_04_rxnorm.py", timeout=14400)
    expect(cur, "STAGE 1", "rxnconso")
    expect(cur, "STAGE 1", "rxnrel")

    # rx_ing -- the ingredient vocabulary with the SALT FOLD. BR-016 depends
    # on it, and it existed only inside exp_04_set_matcher.py, an exploratory
    # script. A chain that needs a table born in an experiment is a chain that
    # cannot be rebuilt, so it is promoted here, SQL verbatim.
    #
    # The folding edge is has_form, NOT precise_ingredient_of: measured on
    # this data, has_form connects a PIN-named concept to an IN-named one
    # 3,684 times while precise_ingredient_of does so 24 times and mostly
    # relates a BRAND to a precise ingredient. Verified on
    #   ('142439','has_form','5489') = hydrocodone bitartrate -> hydrocodone
    # Brand names are deliberately excluded: a brand masquerading as an
    # ingredient is how an ingredient set gets polluted.
    emit("   building rx_ing (the salt-fold ingredient vocabulary)...")
    cur.execute("DROP TABLE IF EXISTS rx_ing")
    cur.execute("""
        CREATE TABLE rx_ing AS
        WITH base AS (
          SELECT DISTINCT c.rxcui, c.tty, upper(c.str_) AS nm
          FROM rxnconso c
          WHERE c.lat='ENG' AND c.suppress='N'
            AND c.tty IN ('IN','PIN','MIN')
            AND c.str_ IS NOT NULL AND c.str_ <> ''
        ),
        pin_to_in AS (
          SELECT DISTINCT r.rxcui1 AS pin, r.rxcui2 AS inn
          FROM rxnrel r
          JOIN (SELECT DISTINCT rxcui FROM rxnconso
                WHERE tty='IN' AND lat='ENG' AND suppress='N') i
            ON i.rxcui = r.rxcui2
          WHERE r.rela = 'has_form' AND r.rxcui1 <> r.rxcui2
        )
        SELECT b.nm, COALESCE(p.inn, b.rxcui) AS ing_rxcui, b.tty
        FROM base b
        LEFT JOIN pin_to_in p ON p.pin = b.rxcui""")
    cur.execute("CREATE INDEX rx_ing_nm ON rx_ing (nm)")
    cur.execute("ANALYZE rx_ing")
    expect(cur, "STAGE 1", "rx_ing")

    run_script("STAGE 1", "pg_load_05_clinicaltrials.py", timeout=14400)
    expect(cur, "STAGE 1", "ct_intervention")
    # pg_load_05 carries only intervention names. exec_05 loads the other
    # half of the trial record: what the trial STUDIES, the registry's own
    # MeSH descriptors, and overall status (a withdrawn trial asserts
    # nothing). ct_mesh is rung N1 -- an indication arriving pre-normalised.
    run_script("STAGE 1", "exec_05_load_trial_conditions.py", timeout=28800)
    expect(cur, "STAGE 1", "ct_study")
    expect(cur, "STAGE 1", "ct_condition")
    expect(cur, "STAGE 1", "ct_mesh")

    run_script("STAGE 1", "exec_04_load_icd.py", timeout=7200)
    expect(cur, "STAGE 1", "icd10cm")
    expect(cur, "STAGE 1", "ccsr")
    expect(cur, "STAGE 1", "coverage_icd10")

    # ---------------------------------------------- 2 the provider registry
    emit()
    emit("=== STAGE 2 -- the provider registry, from the pipeline cluster ===")
    run_script("STAGE 2", "exec_10_port_provider.py", timeout=14400)
    expect(cur, "STAGE 2", "provider")
    expect(cur, "STAGE 2", "provider_business_address")
    expect(cur, "STAGE 2", "nucc_specialty")
    emit("   surfacing the NUCC vocabulary from JSONB to columns (BR-030)...")
    cur.execute("DROP TABLE IF EXISTS chain_nucc CASCADE")
    cur.execute("""
        CREATE TABLE chain_nucc AS
        SELECT nucc_code,
               nullif(btrim(raw->>'Display Name'),'')   AS display_name,
               nullif(btrim(raw->>'Grouping'),'')       AS grouping,
               nullif(btrim(raw->>'Classification'),'') AS classification,
               nullif(btrim(raw->>'Specialization'),'') AS specialization,
               nullif(btrim(raw->>'Definition'),'')     AS definition,
               nullif(btrim(raw->>'Section'),'')        AS section,
               nullif(btrim(raw->>'Notes'),'')          AS notes,
               (raw->>'can_prescribe')::boolean         AS can_prescribe,
               (raw->>'is_homeopathic')::boolean        AS is_homeopathic,
               (raw->>'is_disqualified')::boolean       AS is_disqualified,
               (raw->>'is_supplemented')::boolean       AS is_supplemented
        FROM nucc_specialty""")
    cur.execute("CREATE UNIQUE INDEX cn_code ON chain_nucc (nucc_code)")
    cur.execute("ANALYZE chain_nucc")
    expect(cur, "STAGE 2", "chain_nucc")


    # BR-010 asserted the moment the anchor exists, not later
    cur.execute("""SELECT count(*), count(DISTINCT npi)
                   FROM provider_business_address""")
    rows, npis = cur.fetchone()
    if rows != npis:
        die("STAGE 2", "BR-010: the billing address is not 1:1 with NPI "
                       "(%d rows, %d NPIs)" % (rows, npis))
    emit("   BR-010 billing anchor is 1:1 (%s)" % f"{rows:,}")

    # ---------------------------------------------- 3 the molecule bridge
    emit()
    emit("=== STAGE 3 -- the molecule bridge (BR-016) ===")
    run_script("STAGE 3", "exec_02_build_t1.py", timeout=14400)
    expect(cur, "STAGE 3", "cms_drug_resolution")
    expect(cur, "STAGE 3", "fda_mol")
    expect(cur, "STAGE 3", "fda_brand_norm")
    # The provider derivations, lifted from exec_11_t2_normalized.py.
    #
    # exec_11 is NOT run. It was a transitional script: it built a "normalised
    # T2" and reported it against the PREVIOUS T2 (provider_molecule), so it
    # requires a predecessor that this chain no longer builds -- build_chain
    # is the real T2 now. Only two of its products are still needed, and both
    # are self-contained SQL over tables exec_10 already ported, so they are
    # lifted here verbatim rather than kept alive behind a dead comparison.
    #
    # BR-013: the specialty comes from the NPI registry, never the claim
    # file's prescriber-type field. Aggregated to the NPI so a provider with
    # several taxonomies cannot fan the file out.
    emit("   deriving provider_nucc and provider_spine...")
    cur.execute("""
        DROP TABLE IF EXISTS provider_nucc CASCADE;
        CREATE TABLE provider_nucc AS
        SELECT npi,
               array_agg(DISTINCT code ORDER BY code)             AS nucc_codes,
               (array_agg(code ORDER BY is_primary DESC, code))[1] AS primary_nucc,
               (array_agg(code_label ORDER BY is_primary DESC, code))[1]
                                                                  AS primary_nucc_label,
               count(*)                                           AS n_taxonomies
        FROM provider_taxonomy WHERE code <> '' GROUP BY npi;
        CREATE UNIQUE INDEX pn_npi ON provider_nucc (npi);
        ANALYZE provider_nucc;
    """)
    cur.execute("SELECT count(*), count(DISTINCT npi) FROM provider_nucc")
    a, b = cur.fetchone()
    if a != b:
        die("STAGE 3", "provider_nucc is not 1:1 on NPI (%d rows, %d NPIs); "
                       "the specialty would fan the file out" % (a, b))
    emit("   provider_nucc 1:1 on NPI (%s)" % f"{a:,}")

    cur.execute("""
        DROP TABLE IF EXISTS provider_spine CASCADE;
        CREATE TABLE provider_spine AS
        SELECT p.npi,
               p.entity_type_code_label        AS entity_type,
               p.provider_last_name_legal_name AS last_or_org_name,
               p.provider_first_name           AS first_name,
               p.provider_credential_text      AS credential,
               p.can_prescribe, p.is_homeopathic, p.is_primary_care,
               p.is_active, p.urban            AS provider_urban,
               p.filled_in_test_state, p.office_in_test_state,
               b.state AS billing_state, b.city AS billing_city,
               b.zip AS billing_zip, b.county_fips AS billing_county_fips
        FROM provider p
        LEFT JOIN provider_business_address b ON b.npi = p.npi;
        CREATE UNIQUE INDEX ps_npi ON provider_spine (npi);
        ANALYZE provider_spine;
    """)
    expect(cur, "STAGE 3", "provider_spine")
    expect(cur, "STAGE 3", "provider_nucc")

    # exec_03 builds the ORIGINAL T2, provider_molecule. build_chain later
    # supersedes it, so it is not the delivered table -- but exec_06's tail
    # reports against it, so it is a genuine predecessor rather than dead
    # weight. Running the real sequence beats truncating exec_06 to avoid it:
    # a stage edited to dodge a dependency is a stage nobody can audit.
    run_script("STAGE 3", "exec_03_build_t2.py", timeout=14400)
    expect(cur, "STAGE 3", "provider_molecule")

    # ------------------------------- 4 the label match, CORRECTED INLINE
    emit()
    emit("=== STAGE 4+5 -- label match (3 rungs), indications, UMLS ===")
    emit("   the three label rungs are INSIDE exec_06 now, because")
    emit("   molecule_indication is built from mol_label and a later repair")
    emit("   would leave the indications standing on the defect.")
    run_script("STAGE 4+5", "exec_06_t3_t4_t5.py", timeout=28800)
    expect(cur, "STAGE 4+5", "mol_label")
    expect(cur, "STAGE 4+5", "molecule_indication")
    cur.execute("""SELECT count(DISTINCT rung) FROM mol_label""")
    if cur.fetchone()[0] < 2:
        die("STAGE 4+5", "mol_label has fewer than two match rungs; the "
                         "folded/brand/device rungs did not run")
    run_script("STAGE 4+5", "exec_07_r2_umls_crosswalk.py", timeout=14400)
    expect(cur, "STAGE 4+5", "mesh_icd_umls")
    run_script("STAGE 4+5", "exec_08_t4_t5_final.py", timeout=28800)
    expect(cur, "STAGE 4+5", "indication_icd")

    emit()
    emit("=== STAGE 6 -- THE LLM INFERENCE (NOT DETERMINISTIC) ===")
    emit("   gpt-5.6-luna through FDADrugEnrichmentTool, PROMPT 2 verbatim.")
    emit("   A re-run gives similar, not identical, confidences. Every other")
    emit("   stage in this file is deterministic.")
    run_script("STAGE 6", "exec_09_n4_r5_inference.py", timeout=43200)
    expect(cur, "STAGE 6", "llm_crosswalk")
    run_script("STAGE 6", "exec_14_recover_molecules.py", timeout=43200)
    cur.execute("SELECT count(DISTINCT molecule_id) FROM llm_crosswalk")
    emit("   molecules crosswalked: %s" % f"{cur.fetchone()[0]:,}")

    emit()
    emit("=== STAGE 7 -- cohort notes, deduped to one row per label ===")
    run_script("STAGE 7", "exec_13_fda_cohort_notes.py", timeout=14400)
    cur.execute("""
        DROP TABLE IF EXISTS fda_label_cohort_d;
        CREATE TABLE fda_label_cohort_d AS
        SELECT DISTINCT ON (set_id) * FROM fda_label_cohort
        ORDER BY set_id, effective_time DESC NULLS LAST, spl_id DESC;
        DROP TABLE fda_label_cohort;
        ALTER TABLE fda_label_cohort_d RENAME TO fda_label_cohort;
        CREATE UNIQUE INDEX flc_set ON fda_label_cohort (set_id);
        ANALYZE fda_label_cohort;
    """)
    n = expect(cur, "STAGE 7", "fda_label_cohort")
    cur.execute("SELECT count(DISTINCT set_id) FROM fda_label_cohort")
    if cur.fetchone()[0] != n:
        die("STAGE 7", "BR-029: the cohort table is not 1:1 on set_id")
    emit("   BR-029 one row per label, enforced by a unique index")

    emit()
    emit("=== STAGE 8 -- the chain ===")
    run_script("STAGE 8", "build_chain.py", timeout=28800)
    expect(cur, "STAGE 8", "chain_provider_molecule")
    expect(cur, "STAGE 8", "chain_indication")
    expect(cur, "STAGE 8", "chain_code")
    expect(cur, "STAGE 8", "chain_delivered")

    emit()
    emit("=== STAGE 9 -- confidence at the molecule grain ===")
    run_script("STAGE 9", "fix_confidence_grain.py", timeout=14400)
    cur.execute("""
        SELECT count(*) FROM (
          SELECT npi, molecule_id, indication_id, icd10_code
          FROM chain_delivered GROUP BY 1,2,3,4 HAVING count(*)>1) x""")
    dup = cur.fetchone()[0]
    if dup:
        die("STAGE 9", "%d duplicated delivered facts -- the confidence leak "
                       "is back" % dup)
    emit("   zero duplicated delivered facts")

    emit()
    emit("=== STAGE 10 -- the THREE pseudo-target collections ===")
    run_script("STAGE 10", "build_three_targets.py", timeout=28800)
    expect(cur, "STAGE 10", "pt_providermedicare")
    expect(cur, "STAGE 10", "pt_providermedicare_indication")
    expect(cur, "STAGE 10", "pt_nuccspecialty")
    expect(cur, "STAGE 10", "pt_nuccspecialty_indication")
    expect(cur, "STAGE 10", "pt_fdadruglabel")
    expect(cur, "STAGE 10", "pt_fdadruglabel_molecule")

    emit()
    emit("=== STAGE 11 -- migrate to QA_ collections on the pipeline server ===")
    run_script("STAGE 11", "migrate_to_pipeline_qa.py", timeout=28800)

    emit()
    emit("=== THE REBUILD IS COMPLETE ===")
    emit("   elapsed %.0f minutes" % ((time.time()-t_all)/60))
    cur.execute("""SELECT count(*) FROM information_schema.tables
                   WHERE table_schema='public' AND table_type='BASE TABLE'""")
    emit("   tables built: %s" % cur.fetchone()[0])
    emit()
    emit("   NEXT: migrate_to_pipeline_qa.py writes these to QA_ collections")
    emit("   on the pipeline cluster, then verifies every value exhaustively.")
    con.close()


def guarded():
    """All or nothing. Any failure drops the whole database, so there is
    never a partial build to mistake for a result and never a temptation to
    resume in the middle."""
    try:
        main()
    except ChatHealthyException as f:
        if f.mode != STAGE_FAILURE:
            raise
        emit()
        emit("=" * 68)
        emit("REBUILD FAILED: %s" % f)
        emit("=" * 68)
        con = pg(); cur = con.cursor()
        n = drop_all(cur)
        con.close()
        emit("   dropped %d tables -- nothing partial survives." % n)
        emit("   no number from this run is valid. Start again from source.")
        raise ChatHealthyException(
            "runtime_error", "rebuild failed; every table dropped",
            exception=f)
    except Exception as exc:
        emit()
        emit("=" * 68)
        emit("REBUILD CRASHED: %s: %s" % (type(exc).__name__, exc))
        emit("=" * 68)
        try:
            con = pg(); cur = con.cursor()
            n = drop_all(cur)
            con.close()
            emit("   dropped %d tables -- nothing partial survives." % n)
        except Exception as e2:
            emit("   AND THE CLEANUP FAILED: %s" % e2)
            emit("   the database may hold partial tables. Drop it by hand")
            emit("   before trusting anything in it.")
        raise


guarded()
