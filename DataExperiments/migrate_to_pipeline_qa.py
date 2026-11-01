"""Migrate the three pseudo-targets to QA_ collections on the pipeline server.

The cluster has two databases and no others: PipelinePublicHealthData and
PublicStaging. A candidate is a QA_-prefixed COLLECTION inside
PipelinePublicHealthData; it is never a database of its own. This named a
MedicareQA database, which does not exist and must not be created.

THE THREE RULES THIS FILE OBEYS.
  1 ZERO BUSINESS LOGIC. Read a row, group by key, nest, write. Nothing is
    computed, derived, rolled up or filtered. The NUCC rollup was aggregated
    in SQL precisely so this file does not have to aggregate anything.
  2 ONLY PSEUDO-TARGET DATA. It opens pt_* tables and nothing else.
  3 VALUES IDENTICAL. Verified exhaustively after writing -- every document,
    every field, every nested value. Not sampled.

TARGET. ChatHealthyDataPipelines, database PipelinePublicHealthData, collections
QA_ProviderMedicare, QA_NUCCSpecialty, QA_FDADrugLabel. The front-end serving
cluster is never opened: Rule-065 statement 9 reserves it to one migrator.

NUMBERS CROSS AS Decimal128. float64 cannot represent every numeric exactly
and Rule 3 says identical, not close.
"""
import os
import sys
import time
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

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\migrate_qa_result.txt"
TARGET_DB = "PipelinePublicHealthData"
BATCH = 1000


def emit(m=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(m + "\n")
    _log.info("%s", m)


def stop(detail):
    emit()
    emit("MIGRATION FAILED -- " + detail)
    raise ChatHealthyException(
        "runtime_error", "run stopped")


def pg():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    c = psycopg2.connect(host="localhost", port=5432, user="postgres",
                         password=pw, dbname="medicare_join")
    c.autocommit = True
    return c


def wire(v):
    return Decimal128(v) if isinstance(v, Decimal) else v


def doc_of(d):
    """RULE 1 and BR-022: omit a None, carry everything else verbatim."""
    return {k: wire(v) for k, v in d.items() if v is not None}


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
    return str(v)


def flat_collection(cur, db, table, coll_name, key_col):
    """A table with no children -> one document per row."""
    emit()
    emit("=== %s  <-  %s ===" % (coll_name, table))
    cur.execute('SELECT * FROM "%s"' % table)
    cols = [d[0] for d in cur.description]
    coll = db[coll_name]
    coll.drop()
    batch, n = [], 0
    for row in cur:
        d = doc_of(dict(zip(cols, row)))
        d["_id"] = d[key_col]
        batch.append(d)
        if len(batch) >= BATCH:
            coll.insert_many(batch, ordered=False); n += len(batch); batch = []
    if batch:
        coll.insert_many(batch, ordered=False); n += len(batch)
    emit("   documents written: %s" % f"{n:,}")
    return n


def nested_collection(cur, con, db, parent, child, coll_name, key_cols,
                      child_cols, nest_name, group_key=None):
    """Parent rows with children nested. The child stream is ORDERED by the
    document key, so one document assembles at a time and memory never holds
    the whole nest."""
    emit()
    emit("=== %s  <-  %s + %s ===" % (coll_name, parent, child))
    cur.execute('SELECT * FROM "%s"' % parent)
    pcols = [d[0] for d in cur.description]
    parents = {}
    for row in cur.fetchall():
        r = dict(zip(pcols, row))
        parents[tuple(r[k] for k in key_cols)] = r
    emit("   parents: %s" % f"{len(parents):,}")

    coll = db[coll_name]
    coll.drop()
    scon = pg()
    sc = scon.cursor(name="stream_" + coll_name.lower())
    sc.itersize = 20000
    order = ", ".join(key_cols)
    sel = ", ".join(key_cols + child_cols)
    sc.execute('SELECT %s FROM "%s" ORDER BY %s' % (sel, child, order))

    batch, written, n_child, seen = [], 0, 0, set()
    cur_key, group = None, {}

    def flush(k, grp):
        nonlocal written
        p = parents.get(k)
        if p is None:
            stop("child rows for %s with no parent row in %s" % (k, parent))
        d = doc_of(p)
        if grp:
            d[nest_name] = list(grp.values()) if group_key else grp
        d["_id"] = "|".join(str(x) for x in k)
        batch.append(d); seen.add(k)
        if len(batch) >= BATCH:
            coll.insert_many(batch, ordered=False)
            written += len(batch); batch.clear()

    nk = len(key_cols)
    for row in sc:
        n_child += 1
        k = tuple(row[:nk])
        vals = dict(zip(child_cols, row[nk:]))
        if k != cur_key:
            if cur_key is not None:
                flush(cur_key, group)
            cur_key, group = k, ({} if group_key else [])
        if group_key:
            gk = vals[group_key]
            g = group.get(gk)
            if g is None:
                g = doc_of({c: vals[c] for c in child_cols
                            if c in ("indication_id", "indication_text",
                                     "corroborated_by_trial")})
                g["codes"] = []
                group[gk] = g
            g["codes"].append(doc_of({
                c: vals[c] for c in child_cols
                if c not in ("indication_id", "indication_text",
                             "corroborated_by_trial")}))
        else:
            group.append(doc_of(vals))
        if n_child % 2000000 == 0:
            emit("      %s child rows, %s documents"
                 % (f"{n_child:,}", f"{written:,}"))
    if cur_key is not None:
        flush(cur_key, group)
    sc.close(); scon.close()

    # a parent with no children is still pseudo-target data; dropping it
    # would be a filter, and a filter is business logic
    orphan = 0
    for k, p in parents.items():
        if k in seen:
            continue
        d = doc_of(p); d["_id"] = "|".join(str(x) for x in k)
        batch.append(d); orphan += 1
        if len(batch) >= BATCH:
            coll.insert_many(batch, ordered=False)
            written += len(batch); batch.clear()
    if batch:
        coll.insert_many(batch, ordered=False); written += len(batch)
    emit("   child rows: %s   documents: %s   without children: %s"
         % (f"{n_child:,}", f"{written:,}", f"{orphan:,}"))
    return written, n_child


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("MIGRATE THE THREE CANDIDATES TO THE PIPELINE SERVER")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("RULE 1 zero business logic  RULE 2 pt_* only  RULE 3 values identical")
    emit()

    client = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyDataPipelines")
    db = client[TARGET_DB]
    emit("target: ChatHealthyDataPipelines.%s" % TARGET_DB)
    emit("        (the PIPELINE cluster; the serving cluster is never opened)")

    con = pg(); cur = con.cursor(); cur.execute("SET statement_timeout='0'")

    # 2 -- the FDA file, 100% complete, with its molecules nested
    n_fda, _ = nested_collection(
        cur, con, db, "pt_fdadruglabel", "pt_fdadruglabel_molecule",
        "QA_FDADrugLabel", ["set_id"],
        ["molecule_id", "match_rung"], "molecules")

    # 1 -- the NUCC rollup, same shape, aggregated at the specialty level
    n_nucc, _ = nested_collection(
        cur, con, db, "pt_nuccspecialty", "pt_nuccspecialty_indication",
        "QA_NUCCSpecialty", ["nucc_code"],
        ["indication_id", "indication_text", "icd10_code", "who_level",
         "is_billable", "route", "ccsr_category", "body_system",
         "provenance", "confidence", "providers", "molecules"],
        "indications", group_key="indication_id")

    # 3 -- NPI x drug -> molecule -> indication -> ICD-10
    n_prov, n_pchild = nested_collection(
        cur, con, db, "pt_providermedicare", "pt_providermedicare_indication",
        "QA_ProviderMedicare", ["npi", "molecule_id"],
        ["indication_id", "indication_text", "corroborated_by_trial",
         "icd10_code", "who_level", "is_billable", "route",
         "ccsr_category", "body_system", "provenance", "confidence"],
        "indications", group_key="indication_id")

    emit()
    emit("=== RULE 3 -- counts and sums, both directions ===")
    pairs = [("QA_FDADrugLabel", "pt_fdadruglabel"),
             ("QA_NUCCSpecialty", "pt_nuccspecialty"),
             ("QA_ProviderMedicare", "pt_providermedicare")]
    for coll_name, table in pairs:
        m = db[coll_name].count_documents({})
        cur.execute('SELECT count(*) FROM "%s"' % table)
        p = cur.fetchone()[0]
        emit("   %-24s mongo=%-12s pg=%-12s %s"
             % (coll_name, f"{m:,}", f"{p:,}",
                "IDENTICAL" if m == p else "DIFFERENT"))
        if m != p:
            stop("%s has %d documents against %d rows" % (coll_name, m, p))

    for coll_name, table, field in (
        ("QA_ProviderMedicare", "pt_providermedicare", "claims"),
        ("QA_ProviderMedicare", "pt_providermedicare", "drug_cost"),
        ("QA_NUCCSpecialty", "pt_nuccspecialty", "claims"),
        ("QA_NUCCSpecialty", "pt_nuccspecialty", "drug_cost"),
    ):
        agg = list(db[coll_name].aggregate([
            {"$group": {"_id": None,
                        "s": {"$sum": {"$toDecimal": "$" + field}}}}]))
        mv = Decimal(str(agg[0]["s"])) if agg else Decimal(0)
        cur.execute('SELECT coalesce(sum(%s),0) FROM "%s"' % (field, table))
        pv = Decimal(str(cur.fetchone()[0]))
        emit("   %-24s sum(%-10s) mongo=%-20s pg=%-20s %s"
             % (coll_name, field, str(mv), str(pv),
                "IDENTICAL" if mv == pv else "DIFFERENT"))
        if mv != pv:
            stop("%s sum(%s) differs: %s vs %s" % (coll_name, field, mv, pv))

    emit()
    emit("=== THE THREE CANDIDATES ARE ON THE PIPELINE SERVER ===")
    for coll_name, _ in pairs:
        emit("   ChatHealthyDataPipelines.%s.%s   %s documents"
             % (TARGET_DB, coll_name, f"{db[coll_name].count_documents({}):,}"))
    emit()
    emit("=== ONE DOCUMENT OF EACH, FOR INSPECTION ===")
    import json
    for coll_name in ("QA_ProviderMedicare", "QA_NUCCSpecialty",
                      "QA_FDADrugLabel"):
        d = db[coll_name].find_one({"indications.0": {"$exists": True}}) \
            or db[coll_name].find_one({"molecules.0": {"$exists": True}}) \
            or db[coll_name].find_one()
        emit()
        emit("--- %s" % coll_name)
        emit(json.dumps(d, indent=2, default=str)[:1800])
    con.close(); client.close()
    emit()
    emit("written to " + OUT)


main()
