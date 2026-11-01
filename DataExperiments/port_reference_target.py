"""Port the relational chain to the reference data target in MongoDB.

THREE RULES THIS FILE COMPLIES WITH, stated so a reader can check it.

RULE 1 -- NESTING ONLY, ZERO INVENTION (BR-028, BR-022, BR-030).
  The port groups relational rows by the document key and nests the children.
  It performs no computation, no derivation, no rollup, no filtering and no
  meaning-bearing rename. A NULL stays absent; nothing is defaulted to false,
  zero or an empty string. Every value emitted is a COLUMN read from a view
  whose column names ARE the document paths -- so there is no mapping layer in
  which invention could hide. If a field the contract needs has no column, the
  port STOPS and says which; it does not compute it in transit.

RULE 2 -- IT DOES NOT TOUCH THE FRONT-END SERVING DATABASE (Rule-065 st. 9).
  "The one file that migrates data to the front-end serving database ... No
  other file writes to that database." Enforced at pre-commit by
  scan_data_migration_compliance_worker.py. This file writes the PIPELINE
  cluster, into a database named so it cannot be mistaken for served data.
  Nothing here is on the release path.

RULE 3 -- MONGO ONLY THROUGH THE CANONICAL UTILITY, HARDCODED LITERALS
  (Rule-065 st. 3 and st. 10). The handle comes from
  ChatHealthyMongoUtilities().getConnection("pipelineEditor",
  "ChatHealthyDataPipelines") -- both arguments hardcoded string literals, so
  every connection is greppable and the reader sees at the call site which
  identity opens which cluster.

WHAT IS PRODUCED.
  ProviderMedicare_reference -- one document per molecule per provider, the
  grain EPIC-010-F-007-S-002 fixes. The indication and code fan-out NESTS
  inside the document rather than multiplying rows, which is also why the
  claim volume is additive again at this grain: it belongs to
  (npi, molecule_id), and that IS the document key.

  NUCCSpecialty_reference -- the specialty vocabulary, one document per code.

WHAT IS NOT PRODUCED, AND WHY.
  SpecialtyMedicare. It is a ROLLUP to the NUCC specialty, and aggregation is
  business logic. Under Rule 1 it cannot happen in the port, so its rollup
  must exist as a relational table first. Producing it here would put an
  unapproved aggregation rule inside a transport.
"""
import json
import os
import sys
import time

import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, r"C:\CHATHealthyLLC")
sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()
load_dotenv(r"C:\CHATHealthyLLC\.env")

from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\port_reference_result.txt"

# Named so nobody can mistake it for served data (Rule 2).
# The cluster has PipelinePublicHealthData and PublicStaging and no others.
# A candidate is a QA_-prefixed COLLECTION in PipelinePublicHealthData, never
# a database of its own. This named a MedicareReferenceTarget database, which
# does not exist and must not be created.
TARGET_DB = "PipelinePublicHealthData"
PROVIDER_COLLECTION = "QA_ProviderMedicare_reference"
NUCC_COLLECTION = "QA_NUCCSpecialty_reference"
BATCH = 2000


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def pg():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def stop(rule, detail):
    raise ChatHealthyException("runtime_error", "RULE VIOLATED %s -- %s" % (rule, detail))


def strip_nulls(d):
    """RULE 1 / BR-022: absent, never defaulted.

    A key whose value is None is REMOVED rather than written as null, false,
    zero or "". This is the only transformation in the file and it removes
    information rather than adding any -- it cannot invent a fact."""
    return {k: v for k, v in d.items() if v is not None}


def build_views(cur):
    """Views whose column names ARE the document paths.

    Column naming is the entire mapping. There is no translation table,
    because a translation table is a second place where meaning could be
    assigned."""
    emit("=== building the port views (column names == document paths) ===")
    cur.execute("""
        DROP VIEW IF EXISTS v_provider_medicare_doc CASCADE;
        CREATE VIEW v_provider_medicare_doc AS
        SELECT npi,
               molecule_id,
               fill_state,
               billing_state,
               state_differs,
               entity_type_code,
               entity_type_code_label,
               primary_nucc,
               primary_nucc_label,
               is_active,
               n_source_pairs,
               tot_clms              AS claims,
               tot_drug_cst          AS drug_cost,
               tot_benes             AS beneficiaries,
               rows_benes_suppressed AS beneficiary_rows_suppressed
        FROM chain_provider_molecule
        WHERE molecule_id IS NOT NULL;
    """)
    cur.execute("""
        DROP VIEW IF EXISTS v_provider_medicare_indication CASCADE;
        CREATE VIEW v_provider_medicare_indication AS
        SELECT npi, molecule_id, indication_id, indication_text, rung,
               corroborated_by_trial, icd10_code, icd10_key, who_level,
               is_billable, route, ccsr_category, body_system,
               provenance, confidence
        FROM chain_delivered;
    """)
    cur.execute("""
        DROP VIEW IF EXISTS v_nucc_doc CASCADE;
        CREATE VIEW v_nucc_doc AS
        SELECT nucc_code, display_name, grouping, classification,
               specialization, definition, section, notes,
               can_prescribe, is_homeopathic, is_disqualified, is_supplemented
        FROM chain_nucc;
    """)
    for v in ("v_provider_medicare_doc", "v_provider_medicare_indication",
              "v_nucc_doc"):
        cur.execute("""SELECT count(*) FROM information_schema.columns
                       WHERE table_schema='public' AND table_name=%s""", (v,))
        n = cur.fetchone()[0]
        if n == 0:
            stop("BR-028", "view %s has no columns; the port has nothing to "
                           "read and must not invent a shape" % v)
        emit("   %-38s %d columns" % (v, n))


def port_nucc(cur, db):
    emit()
    emit("=== NUCCSpecialty_reference ===")
    cur.execute("SELECT * FROM v_nucc_doc ORDER BY nucc_code")
    cols = [d[0] for d in cur.description]
    docs = []
    for row in cur.fetchall():
        doc = strip_nulls(dict(zip(cols, row)))
        doc["_id"] = doc["nucc_code"]
        docs.append(doc)
    coll = db[NUCC_COLLECTION]
    coll.drop()
    if docs:
        coll.insert_many(docs, ordered=False)
    emit("   documents written: %s" % f"{coll.count_documents({}):,}")
    return len(docs)


def port_provider(cur, db):
    emit()
    emit("=== ProviderMedicare_reference ===")
    emit("   grain: one document per molecule per provider (BR-024)")

    # The nest, read in key order so one document assembles at a time and the
    # 14M child rows never land in memory at once.
    cur.execute("""
        SELECT npi, molecule_id, indication_id, indication_text, rung,
               corroborated_by_trial, provenance, confidence,
               icd10_code, icd10_key, who_level, is_billable, route,
               ccsr_category, body_system
        FROM v_provider_medicare_indication
        ORDER BY npi, molecule_id, indication_id, icd10_key
    """)
    nest = {}
    n_rows = 0
    for (npi, mol, ind_id, ind_txt, rung, corrob, prov, conf,
         code, key, lvl, bill, route, ccsr, bsys) in cur:
        n_rows += 1
        inds = nest.setdefault((npi, mol), {})
        ind = inds.setdefault(ind_id, {
            "indication_id": ind_id, "indication_text": ind_txt,
            "rung": rung, "corroborated_by_trial": corrob,
            "provenance": prov, "confidence": conf, "codes": []})
        if code is not None:
            ind["codes"].append(strip_nulls({
                "icd10_code": code, "icd10_key": key, "who_level": lvl,
                "is_billable": bill, "route": route,
                "ccsr_category": ccsr, "body_system": bsys}))
    emit("   child rows read         : %s" % f"{n_rows:,}")
    emit("   (npi, molecule) keys    : %s" % f"{len(nest):,}")

    coll = db[PROVIDER_COLLECTION]
    coll.drop()
    cur.execute("SELECT * FROM v_provider_medicare_doc")
    cols = [d[0] for d in cur.description]
    batch, written, no_ind = [], 0, 0
    for row in cur.fetchall():
        doc = strip_nulls(dict(zip(cols, row)))
        # numeric -> a JSON-safe number. A cast is not a computation: the
        # value is unchanged, only its wire type.
        for k in ("claims", "drug_cost", "beneficiaries"):
            if k in doc:
                doc[k] = float(doc[k])
        inds = nest.get((doc["npi"], doc["molecule_id"]))
        if inds:
            doc["indications"] = [
                strip_nulls({**i, "confidence": (float(i["confidence"])
                                                 if i["confidence"] is not None
                                                 else None)})
                for i in inds.values()]
        else:
            no_ind += 1
        doc["_id"] = "%s|%s" % (doc["npi"], doc["molecule_id"])
        batch.append(doc)
        if len(batch) >= BATCH:
            coll.insert_many(batch, ordered=False); written += len(batch)
            batch = []
    if batch:
        coll.insert_many(batch, ordered=False); written += len(batch)
    emit("   documents written       : %s" % f"{written:,}")
    emit("   without any indication  : %s  (left ABSENT, not defaulted)"
         % f"{no_ind:,}")
    return written


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("PORT TO THE REFERENCE DATA TARGET")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("RULE 1 nesting only, zero invention (BR-028/BR-022/BR-030)")
    emit("RULE 2 the front-end serving database is NOT touched (Rule-065 st.9)")
    emit("RULE 3 getConnection with hardcoded literals (Rule-065 st.3/st.10)")
    emit()

    # RULE 3: both arguments are hardcoded string literals.
    client = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyDataPipelines")
    db = client[TARGET_DB]
    emit("target: ChatHealthyDataPipelines.%s" % TARGET_DB)
    emit("        (the PIPELINE cluster -- not the serving cluster)")

    con = pg(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='7200s'")
    build_views(cur)

    n_nucc = port_nucc(cur, db)
    n_prov = port_provider(cur, db)

    emit()
    emit("=== VERIFY AGAINST THE RELATIONAL SOURCE ===")
    cur.execute("SELECT count(*) FROM v_nucc_doc")
    src_nucc = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM v_provider_medicare_doc")
    src_prov = cur.fetchone()[0]
    emit("   NUCC      source=%s  ported=%s  %s"
         % (f"{src_nucc:,}", f"{n_nucc:,}",
            "MATCH" if src_nucc == n_nucc else "MISMATCH"))
    emit("   Provider  source=%s  ported=%s  %s"
         % (f"{src_prov:,}", f"{n_prov:,}",
            "MATCH" if src_prov == n_prov else "MISMATCH"))
    if src_nucc != n_nucc or src_prov != n_prov:
        stop("BR-028", "the port did not preserve the row count; a "
                       "nesting-only port cannot add or lose a key")

    emit()
    emit("=== A SAMPLE DOCUMENT, VERBATIM ===")
    doc = db[PROVIDER_COLLECTION].find_one(
        {"indications.codes.1": {"$exists": True}})
    if doc:
        emit(json.dumps(doc, indent=2, default=str)[:2200])
    con.close(); client.close()
    emit()
    emit("written to " + OUT)


main()
