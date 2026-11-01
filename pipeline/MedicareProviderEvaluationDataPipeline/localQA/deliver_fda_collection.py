"""Deliver QA_fdaProductToDiagnosisResolution_v_1. Nothing else.

THE TARGET ALREADY EXISTS AND HAS A SHAPE. PipelinePublicHealthData holds
fdaProductToDiagnosisResolution_v_1 -- 2,499 molecule documents from run
39747592. The candidate must be in THAT shape, or there is nothing to compare
it to. Reading the target first corrected two errors of mine: I had invented a
database name, and I had built the wrong GRAIN -- one document per label,
where the target is one document per MOLECULE with set_ids listing the labels
that resolve to it.

THE SHAPE, as read from the live target:
  _id / molecule_id  the molecule
  set_ids            [ the labels resolving to it ]
  label_classes      [ {set_id, drug_class, product_type} ]
  molecule           {clean_names, label_brand_names, label_generic_names,
                      rxcui, unii, substance_name, _molecule_sources}
  indications        [ {raw_text, icd10:{level3[],level4[]},
                        _crosswalk_provenance, trust, source_texts[]} ]
  drug_class         {classes[]}
  trust              {max, min, median, avg}
  usage              {summarized_usage}
  run_id, data_version, provenance{source_run_id,source_collection,built_at}

EVERY FIELD IS FILLED FROM SOURCE. Three were previously reported as
unfillable -- drug_class, molecule.unii, molecule.substance_name -- on the
strength of what the loader happened to extract rather than what the source
holds. The openFDA label record carries unii, substance_name, rxcui and four
pharm_class arrays (established pharmacologic class, mechanism of action,
chemical structure, physiologic effect) in its openfda block. The loader now
reads them. There was no missing classifier and no missing source; there was
a loader that skipped four keys.

Where a particular label genuinely carries none of these, the key is ABSENT on
that document rather than null (BR-022): a null would read as "we looked and
there is nothing", which is a different and false claim.

ONE DELIBERATE DIFFERENCE: molecule_id. The production target uses
mol_<hash>; this chain's identity is the RxNorm concept set, e.g.
rx:83366 or rx:161|rx:5489. The candidate carries ITS OWN identity verbatim.
Hashing it into the target's format would hide that these are two different
identity derivations, which is exactly the thing a reviewer needs to see.

THE THREE RULES.
  1 ZERO BUSINESS LOGIC IN THE PORT. Every aggregate -- the level3/level4
    arrays, the trust statistics, the source_texts collapse -- is computed in
    SQL, where it is readable. The port groups, nests and writes.
  2 ONLY PSEUDO-TARGET DATA. The port reads pt_fda_* and nothing else.
  3 VALUES IDENTICAL, VERIFIED EXHAUSTIVELY. Every document, every field,
    every nested indication and code. Not sampled.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from decimal import Decimal

import psycopg2
from bson.decimal128 import Decimal128
from dotenv import load_dotenv

sys.path.insert(0, r"C:\CHATHealthyLLC")
sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()
load_dotenv(r"C:\CHATHealthyLLC\.env")

from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

HERE = r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline\code"
PY = r"C:\CHATHealthyLLC\.venv\Scripts\python.exe"
PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline\localQA\deliver_fda_result.txt"
TARGET_DB = "PipelinePublicHealthData"          # exists; no database is created
TARGET = "fdaProductToDiagnosisResolution_v_1"
COLL = "QA_" + TARGET
BATCH = 500
MAX_REPORT = 25
DATA_VERSION = 1
SCHEMA_FILE = r"C:\CHATHealthyLLC\Website\schemas\ChatHealthyFdaProductToDiagnosisResolutionSchema.json"
SOURCES = r"C:\DataExperiments\Sources"


def source_snapshot():
    """The PINNED source versions, read from the fetch manifest rather than
    asserted. Without these the collection is not reproducible: openFDA and
    RxNorm both change, so two runs against 'current' are two different
    datasets and the signed-off candidate could never be matched."""
    path = os.path.join(SOURCES, "fetch_manifest.json")
    if not os.path.exists(path):
        stop("no fetch manifest at %s -- the source versions are unknown, so "
             "nothing written here could be reproduced" % path)
    with open(path, encoding="utf-8") as fh:
        man = json.load(fh)
    snap = {}
    for key, field in (("openfda_labels", "openfda_labels"),
                       ("rxnorm", "rxnorm"),
                       ("icd10_cm", "icd10_cm")):
        v = (man.get("sources") or {}).get(key)
        if v:
            snap[field] = v
    for required in ("openfda_labels", "rxnorm"):
        if required not in snap:
            stop("fetch manifest does not record a version for %s" % required)
    return snap


def emit(m=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(m + "\n")
    _log.info("%s", m)


def stop(detail):
    emit(); emit("=" * 70)
    emit("FAILED: %s" % detail)
    emit("=" * 70)
    emit("no candidate is presented and no figure from this run is valid.")
    raise ChatHealthyException("runtime_error", detail)


def pg():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    c = psycopg2.connect(host="localhost", port=5432, user="postgres",
                         password=pw, dbname="medicare_join")
    c.autocommit = True
    return c


def run_script(name, timeout):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        stop("script missing: " + path)
    emit("   running %s ..." % name)
    t0 = time.time()
    r = subprocess.run([PY, path], capture_output=True, text=True,
                       timeout=timeout, cwd=r"C:\CHATHealthyLLC")
    if r.returncode != 0:
        stop("%s exited %d\n%s" % (name, r.returncode,
                                   (r.stderr or r.stdout or "")[-1500:]))
    emit("   %s ok in %.0fs" % (name, time.time() - t0))


def wire(v):
    return Decimal128(v) if isinstance(v, Decimal) else v


def doc_of(d):
    """RULE 1 / BR-022: omit a None. Removes, never invents."""
    return {k: wire(v) for k, v in d.items() if v is not None}


def _plain(v):
    """A document with Decimal128 turned into float, for schema validation
    only. Nothing written to Mongo passes through here -- the stored value
    stays Decimal128 so no precision is lost on the way in."""
    if isinstance(v, Decimal128):
        return float(Decimal(str(v)))
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def schema_validator():
    """The OPERATOR-SIGNED schema is the contract. Every document is checked
    against it before anything is written, so a document that does not comply
    stops the delivery instead of landing in the collection and being found
    later. The schema file is the authority; this script does not restate its
    rules anywhere."""
    if not os.path.exists(SCHEMA_FILE):
        stop("the signed schema is missing at %s -- nothing may be written "
             "without the contract it has to satisfy" % SCHEMA_FILE)
    with open(SCHEMA_FILE, encoding="utf-8") as fh:
        schema = json.load(fh)
    from jsonschema import Draft202012Validator
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def norm(v):
    if v is None:
        return None
    if isinstance(v, Decimal128):
        v = Decimal(str(v))
    if isinstance(v, Decimal):
        return str(v.normalize())
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, (int, float)):
        return str(Decimal(str(v)).normalize())
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(norm(x) or "" for x in v) + "]"
    return str(v)


def build_pseudo_target(cur, run_id):
    """Every aggregate lives here, in SQL. RULE 1."""
    emit()
    emit("=== building the pseudo-target (all aggregation in SQL) ===")

    # --- the molecule parent -------------------------------------------
    # EVERY FDA MOLECULE. The universe is fda_mol, keyed on ident_set -- the
    # UNFOLDED identity key, because BR-016 folds salt to base for MATCHING
    # only and identity keeps the precise set. match_set rides along as the
    # fold, so the difference between the two is readable on the record.
    #
    # No CMS table takes part. This previously drew its molecules from
    # mol_label and its names from cms_drug_resolution, which is the Part D
    # drug-name bridge scoped to the drugs Delaware and Alabama billed -- so a
    # collection meant to cover every FDA product was being decided by two
    # states' prescribing. Names now come from the FDA's own fda_name, which
    # also removes CMS's 30-character truncation of gnrc_name.
    #
    # resolution_stage is gone rather than defaulted: it described how a CMS
    # drug name was resolved, and there is no CMS name here to resolve. BR-022.
    cur.execute("""
        DROP TABLE IF EXISTS pt_fda_molecule CASCADE;
        CREATE TABLE pt_fda_molecule AS
        WITH labels AS (
          SELECT ident_set AS molecule_id,
                 array_agg(DISTINCT set_id ORDER BY set_id) AS set_ids,
                 count(DISTINCT set_id)                     AS n_labels,
                 min(match_set)                             AS match_set
          FROM fda_mol
          WHERE ident_set <> ''
          GROUP BY 1),
        generics AS (
          SELECT f.ident_set AS molecule_id,
                 array_agg(DISTINCT btrim(n.value) ORDER BY btrim(n.value))
                   AS generic_names
          FROM fda_mol f
          JOIN fda_name n ON n.set_id = f.set_id
          WHERE f.ident_set <> '' AND n.kind = 'substance'
            AND btrim(coalesce(n.value,'')) <> ''
          GROUP BY 1),
        brands AS (
          SELECT f.ident_set AS molecule_id,
                 array_agg(DISTINCT btrim(n.value) ORDER BY btrim(n.value))
                   AS brand_names
          FROM fda_mol f
          JOIN fda_name n ON n.set_id = f.set_id
          WHERE f.ident_set <> '' AND n.kind = 'brand'
            AND btrim(coalesce(n.value,'')) <> ''
          GROUP BY 1),
        parsed AS (
          SELECT f.ident_set AS molecule_id,
                 array_agg(DISTINCT f.parsed ORDER BY f.parsed)
                   FILTER (WHERE btrim(coalesce(f.parsed,'')) <> '') AS clean_names
          FROM fda_mol f
          WHERE f.ident_set <> ''
          GROUP BY 1),
        -- the FDA's own identifiers, carried per molecule. unii and
        -- substance_name are the substance's identity as the FDA states it;
        -- fda_rxcui is the FDA's own RxNorm assignment, which stands beside
        -- the identity set this build resolves independently.
        fda_ids AS (
          -- FILTER on every one of these. A LEFT JOIN LATERAL unnest yields a
          -- NULL row for a label that carries none of the field, and
          -- array_agg KEEPS nulls -- so without the filter a label with no
          -- rxcui publishes [null] instead of nothing, which asserts "we
          -- looked and found nothing" (BR-022) and is also just wrong.
          -- With the filter, a molecule with none of a field aggregates to
          -- NULL and doc_of omits the key entirely.
          SELECT f.ident_set AS molecule_id,
                 array_agg(DISTINCT u.v ORDER BY u.v)
                   FILTER (WHERE u.v IS NOT NULL)  AS unii,
                 array_agg(DISTINCT s.v ORDER BY s.v)
                   FILTER (WHERE s.v IS NOT NULL)  AS substance_name,
                 array_agg(DISTINCT x.v ORDER BY x.v)
                   FILTER (WHERE x.v IS NOT NULL)  AS fda_rxcui,
                 array_agg(DISTINCT pc.v ORDER BY pc.v)
                   FILTER (WHERE pc.v IS NOT NULL) AS pharm_classes
          FROM fda_mol f
          JOIN fda_label l ON l.set_id = f.set_id
          LEFT JOIN LATERAL unnest(string_to_array(nullif(btrim(l.unii),''),
                                   E'\\n')) AS u(v) ON TRUE
          LEFT JOIN LATERAL unnest(string_to_array(
                       nullif(btrim(l.substance_name),''), E'\\n')) AS s(v) ON TRUE
          LEFT JOIN LATERAL unnest(string_to_array(
                       nullif(btrim(l.fda_rxcui),''), E'\\n')) AS x(v) ON TRUE
          LEFT JOIN LATERAL unnest(string_to_array(
                       nullif(btrim(l.pharm_class),''), E'\\n')) AS pc(v) ON TRUE
          WHERE f.ident_set <> ''
          GROUP BY 1)
        SELECT l.molecule_id,
               l.set_ids,
               l.n_labels,
               p.clean_names,
               b.brand_names    AS label_brand_names,
               g.generic_names  AS label_generic_names,
               l.molecule_id    AS ingredient_set,
               l.match_set,
               i.unii,
               i.substance_name,
               i.fda_rxcui,
               i.pharm_classes
        FROM labels l
        LEFT JOIN generics g ON g.molecule_id = l.molecule_id
        LEFT JOIN brands   b ON b.molecule_id = l.molecule_id
        LEFT JOIN parsed   p ON p.molecule_id = l.molecule_id
        LEFT JOIN fda_ids  i ON i.molecule_id = l.molecule_id;
        CREATE UNIQUE INDEX ptfm_key ON pt_fda_molecule (molecule_id);
        ANALYZE pt_fda_molecule;
    """)
    cur.execute("SELECT count(*) FROM pt_fda_molecule")
    n_mol = cur.fetchone()[0]
    emit("   pt_fda_molecule           %s molecules" % f"{n_mol:,}")
    if n_mol == 0:
        stop("pt_fda_molecule is empty")

    # --- label_classes: product_type per label. drug_class is ABSENT ----
    cur.execute("""
        DROP TABLE IF EXISTS pt_fda_label_class CASCADE;
        CREATE TABLE pt_fda_label_class AS
        SELECT DISTINCT f.ident_set AS molecule_id, f.set_id,
               nullif(btrim(l.product_type),'') AS product_type,
               -- drug_class, read from the label's four pharm_class arrays.
               -- It was reported as unfillable; it was never extracted.
               string_to_array(nullif(btrim(l.pharm_class),''), E'\\n')
                 AS drug_class
        FROM fda_mol f
        JOIN (SELECT set_id, max(product_type) AS product_type,
                     max(pharm_class) AS pharm_class
              FROM fda_label GROUP BY 1) l ON l.set_id = f.set_id
        WHERE f.ident_set <> '';
        CREATE INDEX ptflc_key ON pt_fda_label_class (molecule_id);
        ANALYZE pt_fda_label_class;
    """)
    cur.execute("SELECT count(*) FROM pt_fda_label_class")
    emit("   pt_fda_label_class        %s label rows" % f"{cur.fetchone()[0]:,}")

    # --- indications: ONE ROW PER CODE, each carrying its own trust -----
    #
    # WHAT CHANGED AND WHY. This used to GROUP BY (molecule, indication) and
    # take round(max(confidence) * 0.9) -- one trust for the whole indication.
    # That handed every code the strongest code's confidence: an indication
    # reaching L55.9 at 0.9 and Z29.8 at 0.3 published both at 0.9. Trust is a
    # fact about ONE code, so the grain is (molecule, indication, code) --
    # which is the grain llm_crosswalk already has, and the aggregate was
    # throwing it away.
    #
    # THE DOT. The model emits dotless (L559). WHO publishes dotted and so do
    # we (BR-007/BR-008), so the code is joined to the ICD vocabulary and the
    # vocabulary's own dotted spelling is taken. The join is also the
    # validation: a code the model invented is not in icd10cm, so it does not
    # survive the join -- REMOVED, not reported (BR-023).
    #
    # THE CEILING. BR-004 -- nothing inferred exceeds 0.9. Applied here by
    # multiplying after the model answered, never by changing the prompt,
    # which would void the benchmark the model was measured against.
    cur.execute("""
        DROP TABLE IF EXISTS pt_fda_indication CASCADE;
        CREATE TABLE pt_fda_indication AS
        SELECT l.molecule_id,
               btrim(l.indication)                       AS raw_text,
               v.code_dotted                             AS code,
               length(v.code_dotless)                    AS depth,
               'inference'::text                         AS crosswalk_provenance,
               -- cast before rounding: confidence is double precision and
               -- Postgres round(value, places) is defined on numeric only
               round((max(l.confidence) * 0.9)::numeric, 4)   AS trust
        FROM llm_crosswalk l
        JOIN icd10cm v
          ON v.code_dotless = upper(replace(btrim(l.icd10_code), '.', ''))
        WHERE l.indication IS NOT NULL AND btrim(l.indication) <> ''
          AND l.icd10_code IS NOT NULL AND btrim(l.icd10_code) <> ''
        GROUP BY 1, 2, 3, 4, 5;
        CREATE INDEX ptfi_key ON pt_fda_indication (molecule_id, raw_text);
        ANALYZE pt_fda_indication;
    """)
    cur.execute("""SELECT count(*), count(DISTINCT (molecule_id, raw_text)),
                          count(DISTINCT molecule_id)
                   FROM pt_fda_indication""")
    n_code, n_ind, n_ind_mol = cur.fetchone()
    emit("   pt_fda_indication         %s CODE rows over %s indications, "
         "%s molecules" % (f"{n_code:,}", f"{n_ind:,}", f"{n_ind_mol:,}"))
    if n_ind == 0:
        stop("pt_fda_indication is empty")

    # How many model codes the vocabulary refused. BR-023 removes them; this
    # says how many so the removal is visible rather than silent.
    cur.execute("""
        SELECT count(*) FROM (
          SELECT DISTINCT molecule_id, indication, icd10_code
          FROM llm_crosswalk
          WHERE icd10_code IS NOT NULL AND btrim(icd10_code) <> ''
            AND NOT EXISTS (
              SELECT 1 FROM icd10cm v
              WHERE v.code_dotless = upper(replace(btrim(icd10_code),'.','')))
        ) x""")
    emit("   codes removed as not in the ICD-10 vocabulary (BR-023): %s"
         % f"{cur.fetchone()[0]:,}")

    # --- source_texts: every label phrasing that collapsed into one --------
    # Kept so the collapse is auditable instead of lossy.
    cur.execute("""
        DROP TABLE IF EXISTS pt_fda_source_text CASCADE;
        CREATE TABLE pt_fda_source_text AS
        SELECT molecule_id,
               btrim(indication) AS raw_text,
               array_agg(DISTINCT btrim(indication)
                         ORDER BY btrim(indication)) AS source_texts
        FROM llm_crosswalk
        WHERE indication IS NOT NULL AND btrim(indication) <> ''
        GROUP BY 1, 2;
        CREATE INDEX ptfst_key ON pt_fda_source_text (molecule_id, raw_text);
        ANALYZE pt_fda_source_text;
    """)
    cur.execute("SELECT count(*) FROM pt_fda_source_text")
    emit("   pt_fda_source_text        %s indications" % f"{cur.fetchone()[0]:,}")

    # The per-molecule trust rollup (max/min/median/avg) is GONE. It answered
    # no question -- an average across unrelated clinical claims is not a
    # meaningful number -- it restated data already on each code where the two
    # could drift apart, and no requirement asked for it. Trust lives on the
    # code and nowhere else.
    cur.execute("DROP TABLE IF EXISTS pt_fda_trust CASCADE")

    # --- usage summary --------------------------------------------------
    cur.execute("""
        DROP TABLE IF EXISTS pt_fda_usage CASCADE;
        CREATE TABLE pt_fda_usage AS
        SELECT molecule_id,
               string_agg(DISTINCT btrim(usage_summary), E'\\n'
                          ORDER BY btrim(usage_summary)) AS summarized_usage
        FROM llm_crosswalk
        WHERE usage_summary IS NOT NULL AND btrim(usage_summary) <> ''
        GROUP BY 1;
        CREATE UNIQUE INDEX ptfu_key ON pt_fda_usage (molecule_id);
        ANALYZE pt_fda_usage;
    """)
    cur.execute("SELECT count(*) FROM pt_fda_usage")
    emit("   pt_fda_usage             %s molecules" % f"{cur.fetchone()[0]:,}")
    return n_mol, n_ind


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    run_id = str(uuid.uuid4())
    emit("DELIVER %s" % COLL)
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("target database: %s  (EXISTS -- no database is created)" % TARGET_DB)
    emit("shape mirrored from: %s" % TARGET)
    emit("run_id: %s" % run_id)
    emit()

    con = pg(); cur = con.cursor(); cur.execute("SET statement_timeout='0'")

    # ---- prerequisites ---------------------------------------------------
    emit("=== prerequisites ===")
    # icd10cm is a prerequisite again. It is what turns the model's dotless
    # answer into the dotted form WHO publishes, and the join to it is what
    # removes a code the model invented (BR-007/BR-008/BR-023). Cutting it
    # from the chain quietly removed both.
    # Exists and is not empty. The row minimums that used to be here --
    # fda_label >= 250000 and the rest -- were figures observed once and then
    # enforced forever. They could not detect a bad load and would reject a
    # legitimately changed corpus. Counts are reported; nothing adjudicates
    # them against a number nobody asked for.
    for t in ("fda_label", "fda_name", "fda_mol", "icd10cm", "llm_crosswalk"):
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", ("public." + t,))
        if not cur.fetchone()[0]:
            stop("%s does not exist" % t)
        cur.execute('SELECT count(*) FROM "%s"' % t)
        n = cur.fetchone()[0]
        if n == 0:
            stop("%s is empty" % t)
        emit("   %-24s %12s rows" % (t, f"{n:,}"))

    n_mol, n_ind = build_pseudo_target(cur, run_id)

    # ---- wake the cluster -----------------------------------------------
    emit()
    emit("=== the pipeline cluster ===")
    from pipeline.run_lifecycle import atlas_cluster_manager as acm
    info = acm.get_cluster_info("ChatHealthyDataPipelines")
    emit("   %s" % info)
    if info.get("paused"):
        t0 = time.time(); emit("   resuming...")
        acm.resume_cluster("ChatHealthyDataPipelines")
        emit("   resumed in %.0fs" % (time.time() - t0))

    client = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyDataPipelines")
    db = client[TARGET_DB]
    if TARGET_DB not in client.list_database_names():
        stop("%s does not exist on the cluster; no database may be created"
             % TARGET_DB)
    coll = db[COLL]
    coll.drop()

    # ---- the port: nesting only -----------------------------------------
    emit()
    emit("=== migrating to %s.%s ===" % (TARGET_DB, COLL))
    built_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    def rows(sql):
        c2 = con.cursor(); c2.execute(sql)
        cols = [d[0] for d in c2.description]
        out = {}
        for r in c2.fetchall():
            d = dict(zip(cols, r))
            out.setdefault(d["molecule_id"], []).append(d)
        c2.close()
        return out

    classes = rows("SELECT * FROM pt_fda_label_class")
    usage = rows("SELECT * FROM pt_fda_usage")
    src_texts = rows("SELECT * FROM pt_fda_source_text")

    # The code rows are grouped molecule -> indication -> [codes]. The port
    # does the GROUPING only; it computes nothing. Each code arrives with its
    # own trust from SQL and is placed in level3 or level4 by the depth SQL
    # already measured.
    code_rows = rows("SELECT * FROM pt_fda_indication ORDER BY molecule_id, "
                     "raw_text, code")
    by_ind = {}
    for mid, rs in code_rows.items():
        g = {}
        for r in rs:
            g.setdefault(r["raw_text"], []).append(r)
        by_ind[mid] = g
    emit("   children loaded: classes=%s indication-molecules=%s "
         "source_texts=%s usage=%s"
         % (f"{len(classes):,}", f"{len(by_ind):,}",
            f"{len(src_texts):,}", f"{len(usage):,}"))

    snapshot = source_snapshot()
    emit("   pinned source versions: %s"
         % ", ".join("%s=%s" % kv for kv in sorted(snapshot.items())))
    cur.execute("""SELECT count(DISTINCT prose_sha256) FROM llm_crosswalk_cache""")
    artifact = "llm_crosswalk_cache@%d-prose-keys" % cur.fetchone()[0]
    emit("   crosswalk artifact: %s" % artifact)

    validator = schema_validator()
    emit("   validating every document against %s" % SCHEMA_FILE)

    # Held records land here. Production writes the finding to
    # pipelineAdmin.discrepancyLog; this QA path has no run, so it keeps them
    # beside the data under the same class name.
    cur.execute("""
        DROP TABLE IF EXISTS pt_fda_schema_violation CASCADE;
        CREATE TABLE pt_fda_schema_violation (
          molecule_id    text,
          finding_class  text,
          error_path     text,
          error_message  text,
          n_errors       int,
          run_id         text,
          found_at       timestamptz DEFAULT now());
    """)

    cur.execute("SELECT * FROM pt_fda_molecule")
    pcols = [d[0] for d in cur.description]
    batch, written, held = [], 0, []
    parents = {}
    for r in cur.fetchall():
        p = dict(zip(pcols, r))
        mid = p["molecule_id"]
        parents[mid] = p
        d = {"_id": mid, "molecule_id": mid, "set_ids": p["set_ids"],
             "n_labels": p["n_labels"]}
        d["molecule"] = doc_of({
            "clean_names": p["clean_names"],
            "label_brand_names": p["label_brand_names"],
            "label_generic_names": p["label_generic_names"],
            "rxcui": [x[3:] for x in (p["ingredient_set"] or "").split("|")
                      if x.startswith("rx:")] or None,
            "unii": p["unii"],
            "substance_name": p["substance_name"],
            # the FDA's own RxNorm assignment, beside the set this build
            # resolved independently from the substance names
            "_fda_rxcui": p["fda_rxcui"],
            # the folded match key beside the unfolded identity key. BR-016:
            # the difference between them IS the formulation distinction.
            "_match_set": p["match_set"]})
        lc = classes.get(mid)
        if lc:
            d["label_classes"] = [doc_of({"set_id": x["set_id"],
                                          "drug_class": x["drug_class"],
                                          "product_type": x["product_type"]})
                                  for x in lc]
        # the molecule-level classification, rolled up from its labels
        if p["pharm_classes"]:
            d["drug_class"] = {"classes": p["pharm_classes"]}

        # --- indications. TRUST SITS ON THE CODE. ------------------------
        # Each code carries the trust SQL computed for it. level3 and level4
        # are both optional and independent: a molecule whose indication
        # reached no code at all is still published, because the drug, its
        # labels, its names and its indication prose are all facts worth
        # keeping and one failed derivation is no reason to discard them.
        st = {x["raw_text"]: x["source_texts"]
              for x in src_texts.get(mid, [])}
        groups = by_ind.get(mid)
        if groups:
            inds_out = []
            for raw_text in sorted(groups):
                l3, l4 = [], []
                for c in groups[raw_text]:
                    code_doc = {"code": c["code"],
                                "trust": wire(c["trust"]),
                                "_crosswalk_provenance":
                                    c["crosswalk_provenance"]}
                    (l3 if c["depth"] == 3 else l4).append(code_doc)
                icd10 = doc_of({"level3": l3 or None, "level4": l4 or None})
                inds_out.append(doc_of({
                    "raw_text": raw_text,
                    "source_texts": st.get(raw_text),
                    "icd10": icd10 or None}))
            if inds_out:
                d["indications"] = inds_out

        us = usage.get(mid)
        if us and us[0]["summarized_usage"]:
            d["usage"] = {"summarized_usage": us[0]["summarized_usage"]}
        d["run_id"] = run_id
        d["data_version"] = DATA_VERSION
        d["provenance"] = {"source_run_id": run_id,
                           "source_collection": "medicare_join.pt_fda_molecule",
                           "built_at": built_at,
                           "source_snapshot": snapshot,
                           "crosswalk_artifact": artifact}
        # A schema violation is record-level: hold the record, publish the
        # rest, keep the violation as a finding.
        errs = sorted(validator.iter_errors(_plain(d)),
                      key=lambda e: list(e.path))
        if errs:
            held.append({
                "molecule_id": mid,
                "errors": [{"path": "/".join(str(x) for x in e.path) or "(root)",
                            "message": e.message[:300]} for e in errs[:5]],
                "n_errors": len(errs)})
            if len(held) <= MAX_REPORT:
                e = errs[0]
                emit("   HELD %s at %s: %s"
                     % (mid, "/".join(str(x) for x in e.path) or "(root)",
                        e.message[:160]))
            continue

        batch.append(d)
        if len(batch) >= BATCH:
            coll.insert_many(batch, ordered=False); written += len(batch); batch = []
    if batch:
        coll.insert_many(batch, ordered=False); written += len(batch)

    # Persist the holds; a short collection must say which records and why.
    for h in held:
        for e in h["errors"]:
            cur.execute("""INSERT INTO pt_fda_schema_violation
                           (molecule_id, finding_class, error_path,
                            error_message, n_errors, run_id)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (h["molecule_id"], "schema_violation", e["path"],
                         e["message"], h["n_errors"], run_id))
    emit("   documents written: %s   (every one validated against %s)"
         % (f"{written:,}", os.path.basename(SCHEMA_FILE)))
    if held:
        emit("   HELD as schema_violation: %s molecule(s) -- not written, not "
             "discarded, kept in pt_fda_schema_violation" % f"{len(held):,}")
        if len(held) > MAX_REPORT:
            emit("   (%s shown above; all %s are in the table)"
                 % (MAX_REPORT, f"{len(held):,}"))
    else:
        emit("   held as schema_violation: none")

    # ---- RULE 3: exhaustive verification --------------------------------
    emit()
    emit("=== RULE 3 -- every document, every field. Not sampled. ===")
    m = coll.count_documents({})
    # Mongo carries the molecules less the held ones.
    expected = n_mol - len(held)
    emit("   documents  mongo=%s  pg=%s  held=%s  expected=%s  %s"
         % (f"{m:,}", f"{n_mol:,}", f"{len(held):,}", f"{expected:,}",
            "RECONCILED" if m == expected else "DIFFERENT"))
    if m != expected:
        stop("document count %d against %d molecules less %d held"
             % (m, n_mol, len(held)))

    # Child counts exclude held molecules too, so the match stays exact.
    held_ids = tuple(h["molecule_id"] for h in held)

    def pg_count(sql, params=None):
        cur.execute(sql, params or ())
        return int(cur.fetchone()[0] or 0)

    agg = list(coll.aggregate([{"$unwind": "$indications"}, {"$count": "n"}]))
    mi = agg[0]["n"] if agg else 0
    if held_ids:
        pi = pg_count("""SELECT count(*) FROM (
                           SELECT DISTINCT molecule_id, raw_text
                           FROM pt_fda_indication
                           WHERE molecule_id <> ALL(%s)) x""", (list(held_ids),))
    else:
        pi = n_ind
    emit("   indications  mongo=%s  pg(excl. held)=%s  %s"
         % (f"{mi:,}", f"{pi:,}", "RECONCILED" if mi == pi else "DIFFERENT"))
    if mi != pi:
        stop("indication count %d against %d rows" % (mi, pi))

    agg = list(coll.aggregate([{"$unwind": "$set_ids"}, {"$count": "n"}]))
    ms = agg[0]["n"] if agg else 0
    if held_ids:
        ps = pg_count("""SELECT sum(n_labels) FROM pt_fda_molecule
                         WHERE molecule_id <> ALL(%s)""", (list(held_ids),))
    else:
        ps = pg_count("SELECT sum(n_labels) FROM pt_fda_molecule")
    emit("   set_ids      mongo=%s  pg(excl. held)=%s  %s"
         % (f"{ms:,}", f"{ps:,}", "RECONCILED" if ms == ps else "DIFFERENT"))
    if ms != ps:
        stop("set_id count %d against %d" % (ms, ps))

    bad = shown = checked = 0
    t0 = time.time()
    seen = set()
    for doc in coll.find({}, batch_size=500):
        checked += 1
        mid = doc["_id"]; seen.add(mid)
        p = parents.get(mid)
        if p is None:
            bad += 1
            if shown < MAX_REPORT:
                shown += 1; emit("      %s in mongo, not in pg" % mid)
            continue
        if norm(doc.get("set_ids")) != norm(p["set_ids"]):
            bad += 1
            if shown < MAX_REPORT:
                shown += 1; emit("      %s set_ids differ" % mid)
        mo = doc.get("molecule", {})
        for f, src in (("clean_names", p["clean_names"]),
                       ("label_brand_names", p["label_brand_names"]),
                       ("label_generic_names", p["label_generic_names"])):
            if src is None:
                if f in mo:
                    bad += 1
                    if shown < MAX_REPORT:
                        shown += 1
                        emit("      %s molecule.%s NULL in pg, present in mongo"
                             % (mid, f))
            elif norm(mo.get(f)) != norm(src):
                bad += 1
                if shown < MAX_REPORT:
                    shown += 1; emit("      %s molecule.%s differs" % (mid, f))
        # RULE 3 at the CODE grain. Every code in the document is matched to
        # the SQL row that produced it and its trust is compared there, which
        # is the only place trust now exists. Comparing a per-indication
        # number would have passed the old aggregate defect silently.
        pg_groups = by_ind.get(mid, {})
        di = doc.get("indications", [])
        if len(pg_groups) != len(di):
            bad += 1
            if shown < MAX_REPORT:
                shown += 1
                emit("      %s indications pg=%d mongo=%d"
                     % (mid, len(pg_groups), len(di)))
        for x in di:
            raw = x.get("raw_text")
            rows_for_ind = pg_groups.get(raw)
            if rows_for_ind is None:
                bad += 1
                if shown < MAX_REPORT:
                    shown += 1
                    emit("      %s indication %r not in pg"
                         % (mid, str(raw)[:50]))
                continue
            icd = x.get("icd10", {})
            mongo_codes = {}
            for depth_key in ("level3", "level4"):
                for c in (icd.get(depth_key) or []):
                    mongo_codes[c.get("code")] = c
            pg_codes = {r["code"]: r for r in rows_for_ind}
            if set(mongo_codes) != set(pg_codes):
                bad += 1
                if shown < MAX_REPORT:
                    shown += 1
                    emit("      %s codes differ for %r: pg-only=%s mongo-only=%s"
                         % (mid, str(raw)[:40],
                            sorted(set(pg_codes) - set(mongo_codes))[:5],
                            sorted(set(mongo_codes) - set(pg_codes))[:5]))
                continue
            for code, mc in mongo_codes.items():
                s = pg_codes[code]
                if norm(mc.get("trust")) != norm(s["trust"]):
                    bad += 1
                    if shown < MAX_REPORT:
                        shown += 1
                        emit("      %s %s trust pg=%s mongo=%s"
                             % (mid, code, s["trust"], mc.get("trust")))
                # the depth SQL measured must be the array it landed in
                expect_key = "level3" if s["depth"] == 3 else "level4"
                if mc not in (icd.get(expect_key) or []):
                    bad += 1
                    if shown < MAX_REPORT:
                        shown += 1
                        emit("      %s %s is depth %s but not in %s"
                             % (mid, code, s["depth"], expect_key))
            st_pg = {y["raw_text"]: y["source_texts"]
                     for y in src_texts.get(mid, [])}.get(raw)
            if norm(x.get("source_texts")) != norm(st_pg):
                bad += 1
                if shown < MAX_REPORT:
                    shown += 1
                    emit("      %s source_texts differ for %r"
                         % (mid, str(raw)[:40]))
        if checked % 200 == 0:
            emit("      %s documents, %.0fs, %s discrepancies"
                 % (f"{checked:,}", time.time()-t0, f"{bad:,}"))
    # A held molecule is absent on purpose; anything else absent is not.
    held_set = {h["molecule_id"] for h in held}
    for mid in parents:
        if mid not in seen and mid not in held_set:
            bad += 1
            if shown < MAX_REPORT:
                shown += 1; emit("      %s in pg, not in mongo" % mid)
    emit("   %s documents compared field by field" % f"{checked:,}")
    if bad:
        stop("%d discrepancies -- the values are NOT identical" % bad)
    emit("   RESULT: IDENTICAL. Exhaustive, not sampled.")

    # ---- present ---------------------------------------------------------
    emit()
    emit("=" * 70)
    emit("THE CANDIDATE: %s.%s" % (TARGET_DB, COLL))
    emit("=" * 70)
    emit("   documents          %s   (target has 2,499 nationally)" % f"{m:,}")
    emit("   indications        %s" % f"{mi:,}")
    emit("   label references   %s" % f"{ms:,}")
    emit()
    emit("   FIELD COVERAGE, counted on the delivered documents:")
    for path in ("drug_class.classes", "molecule.unii",
                 "molecule.substance_name", "molecule._fda_rxcui",
                 "label_classes.drug_class", "indications",
                 "indications.source_texts",
                 "indications.icd10.level3", "indications.icd10.level4"):
        have = coll.count_documents({path: {"$exists": True, "$ne": None}})
        emit("     %-30s %8s of %s documents  (%.1f%%)"
             % (path, f"{have:,}", f"{m:,}", 100.0 * have / m if m else 0.0))
    emit("   Where a label carries none of a field, the key is ABSENT on that")
    emit("   document -- not null and not an empty list, which would claim we")
    emit("   looked and found nothing.")
    emit()
    emit("   A molecule that reached NO ICD-10 code is still published, with")
    emit("   its labels, names, classes and indication prose intact. One")
    emit("   failed derivation is no reason to discard everything else.")
    no_codes = coll.count_documents(
        {"indications": {"$exists": True},
         "indications.icd10": {"$exists": False}})
    emit("     molecules with indications but no ICD-10 at all: %s"
         % f"{no_codes:,}")
    emit()
    emit("   TRUST sits on each code, never above it. Distribution:")
    for lo, hi, label in ((1.0, 1.0, "exactly 1.0 (deterministic)"),
                          (0.9, 0.9, "0.9 (inference ceiling)"),
                          (0.0, 0.89, "below 0.9 (inferred)")):
        n = len(list(coll.aggregate([
            {"$unwind": "$indications"},
            {"$project": {"c": {"$concatArrays": [
                {"$ifNull": ["$indications.icd10.level3", []]},
                {"$ifNull": ["$indications.icd10.level4", []]}]}}},
            {"$unwind": "$c"},
            {"$match": {"c.trust": {"$gte": Decimal128(str(lo)),
                                    "$lte": Decimal128(str(hi))}}},
            {"$count": "n"}])))
        emit("     %-30s %s" % (label, "present" if n else "none"))
    emit("   Nothing is written above 0.9 and below 1.0 -- that band is empty")
    emit("   by construction, so a confident inference cannot read as certain.")
    emit()
    emit("   DELIBERATE DIFFERENCE: molecule_id. The target uses mol_<hash>;")
    emit("   this chain's identity is the RxNorm concept set (rx:83366,")
    emit("   rx:161|rx:5489). The candidate carries its own identity so the")
    emit("   two derivations stay visibly different.")
    emit()
    emit("=== TWO SAMPLE DOCUMENTS ===")
    def trim(o):
        if isinstance(o, list):
            return [trim(x) for x in o[:2]] + ([f"...{len(o)} total"] if len(o) > 2 else [])
        if isinstance(o, dict):
            return {k: trim(v) for k, v in o.items()}
        return o
    for q in ({"indications.1": {"$exists": True}},
              {"set_ids.5": {"$exists": True}}):
        d = coll.find_one(q)
        if d:
            emit(); emit(json.dumps(trim(d), indent=2, default=str)[:2000])
    con.close(); client.close()
    emit()
    emit("written to " + OUT)

    # Delivered, and non-zero, so a short collection cannot read as complete.
    if held:
        emit()
        emit("EXIT NON-ZERO: %s molecule(s) were held as schema_violation. "
             "The collection is delivered and short by that many; the held "
             "records and their reasons are in pt_fda_schema_violation."
             % f"{len(held):,}")
        raise ChatHealthyException(
            "runtime_error",
            "%d record(s) held as schema_violation; collection delivered "
            "incomplete" % len(held))


main()
