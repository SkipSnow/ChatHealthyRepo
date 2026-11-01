"""Execution step 9 -- run the inference the design calls for, N4 and R5.

The design marks two inference points and I ran neither. They are run here with
the ACCEPTED artifacts, not with anything invented in this experiment:

  tool    FDADrugEnrichmentTool, PROMPT 2 verbatim from Appendix C
  model   gpt-5.6-luna, from CH_FDA_ENRICHMENT_MODEL, the benchmark's choice
  catalog pipeline/MedicareProviderEvaluationDataPipeline/data/
          icd10_categories_top3levels.xlsx -- byte-identical to the catalog the
          benchmark proved, used as the model's cached static prefix
  levers  crosswalk_batch_size 25, max_transient_retries 5, backoff_base_s 2,
          from pipeline_config

The benchmark is the authority for this link, not this experiment:
  EPIC-010-F-007-S-008, Medicare_IndicationMap_Benchmark_Deliverable.zip
  gpt-5.6-luna: hallucination 0.000, coverage 0.982, legal-correctness 0.933,
  consistency 0.882, $0.79/1k. Pass 3 on 750 real labels: crosswalk coverage
  733/750 = 97.7% AFTER retest-on-drop (93% before; luna dropped 45, retest
  recovered 39).

RETEST-ON-DROP IS MANDATORY per the test plan section 5 -- luna drops ~6% of
batch ids and the recovery is what takes 93% to 97.7%. It is implemented here,
not assumed.

PROMPT 2 performs N4 and R5 in one call: it takes a molecule and its raw
indication prose and returns DISCRETE indications each with catalog codes at
levels 3 and 4. So extraction, canonicalisation and the crosswalk are the same
sanctioned call, exactly as benchmarked.

Persist-first: every returned record is written before the next batch is fired,
so a paid result is never discarded.
"""
import hashlib
import io
import json
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
OUT = r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline\code\exec_09_inference_result.txt"
CATALOG = os.path.join(
    r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline",
    "data", "icd10_categories_top3levels.xlsx")
BATCH = 25
RETRIES = 5
BACKOFF = 2.0
MAX_PROSE = 12000      # one molecule's unioned prose, capped so a batch fits


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
    emit("EXECUTION STEP 9 -- N4 + R5 via the benchmarked tool")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("model=%s  catalog=%s" % (os.environ.get("CH_FDA_ENRICHMENT_MODEL"),
                                   os.path.basename(CATALOG)))
    emit("authority: EPIC-010-F-007-S-008 benchmark; Pass 3 = 97.7% after "
         "retest-on-drop")
    emit()

    con = connect(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='3600s'")

    # ---- the inference input: one record per molecule, unioned prose --------
    # This is the benchmark's Test-bed-1/3 shape: unioned indication text per
    # molecule, which is the shape luna was scored on.
    # The molecule universe is the FDA's, so it comes from fda_mol and no CMS
    # table takes part. Keyed on ident_set, the UNFOLDED identity key: BR-016
    # folds salt to base for MATCHING only, and identity keeps the precise set.
    # The drug's name is the FDA substance name for the same reason -- CMS's
    # gnrc_name is hard-truncated at 30 characters.
    cur.execute("""
        SELECT f.ident_set AS molecule_id,
               min(nm.value) AS a_name,
               left(string_agg(DISTINCT nullif(l.indications_and_usage,''), E'\\n'),
                    %s) AS prose
        FROM fda_mol f
        JOIN (SELECT DISTINCT set_id, indications_and_usage FROM fda_label) l
          ON l.set_id = f.set_id
        LEFT JOIN (SELECT set_id, min(value) AS value FROM fda_name
                   WHERE kind='substance' AND value <> '' GROUP BY set_id) nm
          ON nm.set_id = f.set_id
        WHERE f.ident_set <> ''
        GROUP BY 1
        HAVING left(string_agg(DISTINCT nullif(l.indications_and_usage,''), E'\\n'), %s) <> ''
        ORDER BY 1
    """, (MAX_PROSE, MAX_PROSE))
    rows = cur.fetchall()
    emit("molecules with indication prose to crosswalk: %s" % f"{len(rows):,}")

    records = [{"id": mol, "set_id": mol,
                "molecule": (name or mol), "indication_text": prose}
               for mol, name, prose in rows if (prose or "").strip()]
    emit("inference records prepared: %s" % f"{len(records):,}")

    cur.execute("""
        DROP TABLE IF EXISTS llm_crosswalk;
        CREATE TABLE llm_crosswalk (
          molecule_id text, indication text, confidence_rationale text,
          icd10_code text, level int, confidence double precision,
          code_description text, usage_summary text, attempt int);
    """)

    # ---- THE DURABLE CROSSWALK. --------------------------------------------
    # The collection the operator signs off on has to be the collection the
    # pipeline reproduces. A model asked the same question twice does not give
    # the same answer twice, so an inference regenerated per run can never
    # match a signed-off candidate. The answer is therefore DATA: keyed by the
    # molecule and by a hash of the exact prose that produced it, written once,
    # and read on every later run. Re-running this step reproduces the
    # candidate byte for byte; only prose that has CHANGED reaches the model.
    #
    # This table is deliberately NOT dropped. It is the artifact that travels
    # to the pipeline, and deleting it would discard the signed-off answer.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS llm_crosswalk_cache (
          molecule_id text, prose_sha256 text, indication text,
          confidence_rationale text, icd10_code text, level int,
          confidence double precision, code_description text,
          usage_summary text, attempt int, inferred_at timestamptz
                                             DEFAULT now());
        CREATE INDEX IF NOT EXISTS lcc_key
          ON llm_crosswalk_cache (molecule_id, prose_sha256);
    """)

    def prose_key(text):
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    wanted_keys = {r["set_id"]: prose_key(r["indication_text"])
                   for r in records}
    cur.execute("""SELECT DISTINCT molecule_id, prose_sha256
                   FROM llm_crosswalk_cache""")
    cached = {(m, h) for m, h in cur.fetchall()}
    fresh = [r for r in records
             if (r["set_id"], wanted_keys[r["set_id"]]) not in cached]
    emit()
    emit("=== the durable crosswalk ===")
    emit("  molecules to deliver          %s" % f"{len(records):,}")
    emit("  already inferred (reused)     %s" % f"{len(records)-len(fresh):,}")
    emit("  going to the model this run   %s" % f"{len(fresh):,}")
    records = fresh

    tool = FDADrugEnrichmentTool(catalog=CATALOG, batch_size=BATCH,
                                 max_transient_retries=RETRIES,
                                 backoff_base_s=BACKOFF)
    emit("catalog codes loaded: %s" % f"{len(tool.catalog_codes):,}")
    emit()

    persisted = {"records": 0, "indications": 0, "codes": 0}
    errors = []

    def sink(record, attempt):
        """The model's answer goes to the CACHE, never straight to the
        delivered table. llm_crosswalk is materialised from the cache once
        every molecule is accounted for, so the delivered set is the same
        whether a molecule was inferred this run or three runs ago."""
        doc = record.model_dump()
        mol = str(doc.get("set_id") or "")
        h = wanted_keys.get(mol, "")
        persisted["records"] += 1
        for ind in (doc.get("indications") or []):
            persisted["indications"] += 1
            codes = ind.get("codes") or []
            if not codes:
                cur.execute("""INSERT INTO llm_crosswalk_cache
                               (molecule_id, prose_sha256, indication,
                                confidence_rationale, icd10_code, level,
                                confidence, code_description, usage_summary,
                                attempt)
                               VALUES (%s,%s,%s,%s,NULL,NULL,NULL,NULL,%s,%s)""",
                            (mol, h, ind.get("indication"),
                             ind.get("confidence_rationale"),
                             doc.get("usage_summary"), attempt))
            for c in codes:
                persisted["codes"] += 1
                cur.execute("""INSERT INTO llm_crosswalk_cache
                               (molecule_id, prose_sha256, indication,
                                confidence_rationale, icd10_code, level,
                                confidence, code_description, usage_summary,
                                attempt)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (mol, h, ind.get("indication"),
                             ind.get("confidence_rationale"),
                             c.get("icd10_code"), c.get("level"),
                             c.get("confidence"), c.get("description"),
                             doc.get("usage_summary"), attempt))

    def run_pass(batch_records, attempt):
        t0 = time.time()
        got = tool.enrich(
            batch_records,
            sink=lambda r: sink(r, attempt),
            on_batch_error=lambda keys, exc: errors.append(
                (attempt, [k.set_id for k in keys], str(exc)[:160])),
            on_batch_usage=lambda m: None)
        emit("  pass %d: fired=%s returned=%s  %.0fs"
             % (attempt, f"{len(batch_records):,}", f"{len(got):,}",
                time.time() - t0))
        return {str(r.set_id) for r in got}

    t0 = time.time()
    returned = run_pass(records, 1) if records else set()
    if not records:
        emit("  every molecule was already inferred; the model was not called")

    # ---- RETEST-ON-DROP, mandatory per test plan section 5 -----------------
    wanted = {r["set_id"] for r in records}
    dropped = sorted(wanted - returned)
    emit()
    emit("=== RETEST-ON-DROP (test plan section 5, mandatory) ===")
    emit("  fired=%s returned=%s dropped=%s (%.1f%%)"
         % (f"{len(wanted):,}", f"{len(returned):,}", f"{len(dropped):,}",
            100.0 * len(dropped) / max(len(wanted), 1)))
    if dropped:
        retry = [r for r in records if r["set_id"] in set(dropped)]
        recovered = run_pass(retry, 2)
        emit("  retest recovered %s of %s dropped"
             % (f"{len(recovered):,}", f"{len(dropped):,}"))
        returned |= recovered
        still = sorted(wanted - returned)
        emit("  still missing after retest: %s" % f"{len(still):,}")
    emit("  total elapsed %.0fs" % (time.time() - t0))

    # ---- materialise the delivered set FROM THE CACHE ----------------------
    # Every molecule is read back from the durable artifact at the prose hash
    # that belongs to it, so a molecule inferred this run and one inferred
    # three runs ago are delivered identically. This is what makes the
    # signed-off candidate reproducible.
    cur.execute("DROP TABLE IF EXISTS tmp_wanted")
    cur.execute("CREATE TABLE tmp_wanted (molecule_id text, prose_sha256 text)")
    wbuf = io.StringIO()
    for mol, h in wanted_keys.items():
        wbuf.write("%s\t%s\n" % (mol, h))
    wbuf.seek(0)
    cur.copy_from(wbuf, "tmp_wanted", columns=("molecule_id", "prose_sha256"))
    cur.execute("CREATE INDEX tw_key ON tmp_wanted (molecule_id, prose_sha256)")
    cur.execute("""
        INSERT INTO llm_crosswalk
        SELECT DISTINCT c.molecule_id, c.indication, c.confidence_rationale,
               c.icd10_code, c.level, c.confidence, c.code_description,
               c.usage_summary, c.attempt
        FROM llm_crosswalk_cache c
        JOIN tmp_wanted w ON w.molecule_id = c.molecule_id
                         AND w.prose_sha256 = c.prose_sha256""")
    cur.execute("DROP TABLE tmp_wanted")
    cur.execute("SELECT count(*), count(DISTINCT molecule_id) FROM llm_crosswalk")
    cw_rows, cw_mols = cur.fetchone()
    emit()
    emit("  delivered from the durable crosswalk: %s rows over %s molecules"
         % (f"{cw_rows:,}", f"{cw_mols:,}"))

    for ddl in ("CREATE INDEX lc_mol ON llm_crosswalk (molecule_id)",
                "CREATE INDEX lc_code ON llm_crosswalk (icd10_code)"):
        cur.execute(ddl)
    cur.execute("ANALYZE llm_crosswalk")

    emit()
    emit("=== WHAT THE INFERENCE PRODUCED ===")
    emit("  records persisted=%s indications=%s codes=%s"
         % (f"{persisted['records']:,}", f"{persisted['indications']:,}",
            f"{persisted['codes']:,}"))
    cur.execute("""SELECT count(DISTINCT molecule_id),
                          count(DISTINCT indication),
                          count(DISTINCT icd10_code),
                          count(*) FILTER (WHERE level=3),
                          count(*) FILTER (WHERE level=4),
                          count(*) FILTER (WHERE icd10_code IS NULL)
                   FROM llm_crosswalk""")
    m, i_, c, l3, l4, nil = cur.fetchone()
    emit("  molecules=%s indications=%s distinct codes=%s  L3=%s L4=%s "
         "indications with NO code=%s"
         % (f"{m:,}", f"{i_:,}", f"{c:,}", f"{l3:,}", f"{l4:,}", f"{nil:,}"))

    emit()
    emit("=== HALLUCINATION CHECK -- codes outside the catalog ===")
    codes_in_catalog = tool.catalog_codes
    cur.execute("SELECT DISTINCT icd10_code FROM llm_crosswalk WHERE icd10_code IS NOT NULL")
    returned_codes = {r[0] for r in cur.fetchall()}
    bad = sorted(c for c in returned_codes
                 if c and c.replace(".", "") not in codes_in_catalog
                 and c not in codes_in_catalog)
    emit("  distinct codes returned=%s   outside the catalog=%s"
         % (f"{len(returned_codes):,}", f"{len(bad):,}"))
    if bad:
        emit("  offenders: %s" % ", ".join(bad[:20]))
    else:
        emit("  zero hallucination, matching the benchmark's 0.000 for luna")

    emit()
    emit("=== COVERAGE, against the benchmark's 97.7% ===")
    cur.execute("SELECT count(DISTINCT molecule_id) FROM llm_crosswalk WHERE icd10_code IS NOT NULL")
    with_code = cur.fetchone()[0]
    emit("  molecules reaching at least one ICD code: %s of %s (%.1f%%)"
         % (f"{with_code:,}", f"{len(records):,}",
            100.0 * with_code / max(len(records), 1)))

    if errors:
        emit()
        emit("=== BATCH ERRORS ===")
        for att, ids, msg in errors[:8]:
            emit("  pass %d  %d ids  %s" % (att, len(ids), msg))

    emit()
    emit("=== SAMPLE OUTPUT ===")
    cur.execute("""SELECT molecule_id, indication, icd10_code, level, confidence,
                          code_description
                   FROM llm_crosswalk WHERE icd10_code IS NOT NULL
                   ORDER BY molecule_id LIMIT 14""")
    for mol, ind, code, lv, conf, desc in cur.fetchall():
        emit("  %-14s %-34s %-9s L%s %.2f  %s"
             % (mol[:14], str(ind)[:34], code, lv, conf or 0, str(desc)[:34]))
    con.close()
    emit()
    emit("written to " + OUT)


main()
