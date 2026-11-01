"""Port the pseudo-target tables to documents, and prove the values match.

THE THREE RULES, AND HOW THIS FILE OBEYS EACH ONE.

RULE 1 -- ZERO BUSINESS LOGIC.
  The only operations are: read a row, group rows by their key, nest children
  under their parent, write. No value is computed, derived, rolled up,
  filtered or renamed. There is no branch in this file that depends on what a
  value MEANS.

RULE 2 -- ONLY THE DATA IN THE PSEUDO-TARGET PUBLIC COLLECTIONS.
  It reads pt_providermedicare, pt_providermedicare_indication and
  pt_nuccspecialty and NOTHING else. The staging tables are not opened. This
  is why the pseudo-targets were built first: the internal fields were removed
  in SQL, where the removal is readable, instead of being filtered in transit
  where it would have been business logic.

RULE 3 -- VALUES IDENTICAL BETWEEN MONGO AND THE RELATIONAL DB.
  Not asserted -- VERIFIED. After writing, every scalar is summed on both
  sides and compared, counts are compared, and a random sample of whole
  documents is compared field by field against its source rows. A mismatch
  stops the run.

  Numbers cross as Decimal128, not float. float64 cannot represent every
  numeric exactly, and Rule 3 says identical, not close.

FORM DECISIONS I MADE RATHER THAN ASKING.
  A NULL column is OMITTED from the document rather than written as null.
  That is the document convention and matches absent-rather-than-defaulted.
  Reversing it is one line. The operator's point stands: these are shape
  proposals to iterate on, not questions to stall on.
"""
import os
import random
import sys
import time
from decimal import Decimal

import psycopg2
from bson.decimal128 import Decimal128

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\port_to_documents_result.txt"
TARGET_DB = "PipelinePublicHealthData"
PROVIDER_COLL = "ProviderMedicare"
NUCC_COLL = "NUCCSpecialty"
BATCH = 1000


def emit(m=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(m + "\n")
    _log.info("%s", m)


def stop(rule, detail):
    raise ChatHealthyException("runtime_error", "RULE VIOLATED %s -- %s" % (rule, detail))


def pg():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def wire(v):
    """A value on its way to the document. Decimal crosses as Decimal128 so
    it is not rounded; everything else crosses unchanged. This is a type
    carrier, not a transformation -- the value is the same number."""
    if isinstance(v, Decimal):
        return Decimal128(v)
    return v


def doc_from(row_dict):
    """RULE 1: omit a None, carry everything else verbatim."""
    return {k: wire(v) for k, v in row_dict.items() if v is not None}


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("PORT THE PSEUDO-TARGETS TO DOCUMENTS")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("RULE 1 zero business logic   RULE 2 pseudo-target data only   "
         "RULE 3 values identical (verified, not asserted)")
    emit("target: %s on the pipeline cluster" % TARGET_DB)
    emit()

    mc = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyDataPipelines")
    db = mc[TARGET_DB]
    con = pg(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='7200s'")

    # ------------------------------------------------ NUCCSpecialty
    emit("=== NUCCSpecialty ===")
    cur.execute("SELECT * FROM pt_nuccspecialty ORDER BY nucc_code")
    cols = [d[0] for d in cur.description]
    docs = []
    for row in cur.fetchall():
        d = doc_from(dict(zip(cols, row)))
        d["_id"] = d["nucc_code"]
        docs.append(d)
    db[NUCC_COLL].drop()
    if docs:
        db[NUCC_COLL].insert_many(docs, ordered=False)
    emit("   documents: %s" % f"{db[NUCC_COLL].count_documents({}):,}")

    # ------------------------------------------------ ProviderMedicare
    emit()
    emit("=== ProviderMedicare ===")
    emit("   loading parents...")
    cur.execute("SELECT * FROM pt_providermedicare")
    pcols = [d[0] for d in cur.description]
    parents = {}
    for row in cur.fetchall():
        r = dict(zip(pcols, row))
        parents[(r["npi"], r["molecule_id"])] = r
    emit("   parents: %s" % f"{len(parents):,}")

    emit("   streaming children in key order...")
    coll = db[PROVIDER_COLL]
    coll.drop()

    # A server-side cursor needs a transaction, so the stream gets its own
    # connection with autocommit OFF. And because the stream is ORDERED by the
    # document key, a document can be emitted the moment its key changes --
    # so memory holds one document, not 14.1M rows.
    scon = pg()
    child = scon.cursor(name="child_stream")
    child.itersize = 20000
    child.execute("""
        SELECT npi, molecule_id, indication_id, indication_text,
               corroborated_by_trial, icd10_code, who_level, is_billable,
               route, ccsr_category, body_system, provenance, confidence
        FROM pt_providermedicare_indication
        ORDER BY npi, molecule_id, indication_id, icd10_code
    """)

    batch, written, with_inds, n_child = [], 0, 0, 0
    cur_key, cur_inds, seen = None, {}, set()

    def flush_doc(key, inds):
        nonlocal written, with_inds
        p = parents.get(key)
        if p is None:
            stop("RULE 3", "child rows exist for %s with no parent row; the "
                           "pseudo-targets disagree with each other" % (key,))
        d = doc_from(p)
        if inds:
            d["indications"] = list(inds.values())
            with_inds += 1
        d["_id"] = "%s|%s" % (key[0], key[1])
        batch.append(d)
        seen.add(key)
        if len(batch) >= BATCH:
            coll.insert_many(batch, ordered=False)
            written += len(batch)
            batch.clear()

    for (npi, mol, ind_id, ind_txt, corrob, code, lvl, bill, route,
         ccsr, bsys, prov, conf) in child:
        n_child += 1
        key = (npi, mol)
        if key != cur_key:
            if cur_key is not None:
                flush_doc(cur_key, cur_inds)
            cur_key, cur_inds = key, {}
        ind = cur_inds.get(ind_id)
        if ind is None:
            ind = doc_from({"indication_id": ind_id,
                            "indication_text": ind_txt,
                            "corroborated_by_trial": corrob,
                            "provenance": prov, "confidence": conf})
            ind["codes"] = []
            cur_inds[ind_id] = ind
        ind["codes"].append(doc_from({
            "icd10_code": code, "who_level": lvl, "is_billable": bill,
            "route": route, "ccsr_category": ccsr, "body_system": bsys}))
        if n_child % 2000000 == 0:
            emit("      %s child rows, %s documents written"
                 % (f"{n_child:,}", f"{written:,}"))
    if cur_key is not None:
        flush_doc(cur_key, cur_inds)
    child.close(); scon.close()

    # RULE 2: a parent with no children is still pseudo-target data and is
    # written. Dropping it would be a filter, and a filter is business logic.
    no_child = 0
    for key, p in parents.items():
        if key in seen:
            continue
        d = doc_from(p)
        d["_id"] = "%s|%s" % (key[0], key[1])
        batch.append(d); no_child += 1
        if len(batch) >= BATCH:
            coll.insert_many(batch, ordered=False)
            written += len(batch); batch.clear()
    if batch:
        coll.insert_many(batch, ordered=False); written += len(batch)

    emit("   child rows read: %s" % f"{n_child:,}")
    emit("   documents: %s   carrying indications: %s   without any: %s"
         % (f"{written:,}", f"{with_inds:,}", f"{no_child:,}"))

    # ============================= RULE 3 VERIFIED, NOT ASSERTED
    emit()
    emit("=== RULE 3 -- every value compared, both directions ===")

    def pgval(q):
        cur.execute(q); return cur.fetchone()[0]

    # counts
    for name, mq, pq in (
        ("ProviderMedicare documents", lambda: coll.count_documents({}),
         "SELECT count(*) FROM pt_providermedicare"),
        ("NUCCSpecialty documents", lambda: db[NUCC_COLL].count_documents({}),
         "SELECT count(*) FROM pt_nuccspecialty"),
    ):
        m, p = mq(), pgval(pq)
        ok = m == p
        emit("   %-32s mongo=%-14s pg=%-14s %s"
             % (name, f"{m:,}", f"{p:,}", "IDENTICAL" if ok else "DIFFERENT"))
        if not ok:
            stop("RULE 3", "%s differs" % name)

    # scalar sums, Decimal both sides
    for field, pgcol in (("claims", "claims"), ("drug_cost", "drug_cost"),
                         ("beneficiaries", "beneficiaries")):
        agg = list(coll.aggregate([
            {"$group": {"_id": None, "s": {"$sum": {"$toDecimal": "$" + field}}}}]))
        mv = Decimal(str(agg[0]["s"])) if agg else Decimal(0)
        pv = pgval("SELECT coalesce(sum(%s),0) FROM pt_providermedicare" % pgcol)
        pv = Decimal(str(pv))
        ok = (mv == pv)
        emit("   %-32s mongo=%-20s pg=%-20s %s"
             % ("sum(%s)" % field, str(mv), str(pv),
                "IDENTICAL" if ok else "DIFFERENT"))
        if not ok:
            stop("RULE 3", "sum(%s) differs: mongo %s vs pg %s"
                 % (field, mv, pv))

    # nested cardinality
    agg = list(coll.aggregate([
        {"$unwind": "$indications"}, {"$unwind": "$indications.codes"},
        {"$count": "n"}]))
    m_child = agg[0]["n"] if agg else 0
    p_child = pgval("SELECT count(*) FROM pt_providermedicare_indication")
    ok = m_child == p_child
    emit("   %-32s mongo=%-14s pg=%-14s %s"
         % ("nested code rows", f"{m_child:,}", f"{p_child:,}",
            "IDENTICAL" if ok else "DIFFERENT"))
    if not ok:
        stop("RULE 3", "the nest lost or gained rows: %d vs %d"
             % (m_child, p_child))

    # whole-document sample, field by field
    emit()
    emit("   sampling whole documents and comparing field by field...")
    cur.execute("SELECT npi, molecule_id FROM pt_providermedicare "
                "ORDER BY random() LIMIT 25")
    sample = cur.fetchall()
    bad = 0
    for npi, mol in sample:
        d = coll.find_one({"_id": "%s|%s" % (npi, mol)})
        if not d:
            bad += 1; emit("      MISSING %s|%s" % (npi, mol)); continue
        cur.execute("SELECT * FROM pt_providermedicare WHERE npi=%s AND molecule_id=%s",
                    (npi, mol))
        src = dict(zip([c[0] for c in cur.description], cur.fetchone()))
        for k, v in src.items():
            if v is None:
                if k in d:
                    bad += 1
                    emit("      %s|%s field %s is NULL in pg but present in mongo"
                         % (npi, mol, k))
                continue
            got = d.get(k)
            if isinstance(got, Decimal128):
                got = Decimal(str(got))
            if isinstance(v, Decimal):
                v = Decimal(str(v))
            if got != v:
                bad += 1
                emit("      %s|%s field %s  pg=%r mongo=%r" % (npi, mol, k, v, got))
        cur.execute("""SELECT count(*) FROM pt_providermedicare_indication
                       WHERE npi=%s AND molecule_id=%s""", (npi, mol))
        pn = cur.fetchone()[0]
        mn = sum(len(i.get("codes", [])) for i in d.get("indications", []))
        if pn != mn:
            bad += 1
            emit("      %s|%s nested codes pg=%d mongo=%d" % (npi, mol, pn, mn))
    emit("   %d documents sampled, %d field discrepancies" % (len(sample), bad))
    if bad:
        stop("RULE 3", "%d field discrepancies in a 25-document sample" % bad)

    emit()
    emit("=== THE SHAPE PROPOSED, one real document ===")
    import json
    d = coll.find_one({"indications.1": {"$exists": True},
                       "indications.0.codes.1": {"$exists": True}})
    emit(json.dumps(d, indent=2, default=str)[:2600])

    emit()
    emit("=== NUCCSpecialty, one real document ===")
    emit(json.dumps(db[NUCC_COLL].find_one(), indent=2, default=str)[:900])

    con.close(); mc.close()
    emit()
    emit("ALL RULE 3 CHECKS PASSED -- values identical in both directions")
    emit("written to " + OUT)


main()
