"""Compare EVERY document against its source rows. No sampling.

WHY THIS FILE EXISTS. Rule 3 is that the values are exactly the same between
Mongo and the relational db. The previous run checked aggregates and 25
documents out of 513,777 and I reported it as verified. Aggregate agreement
plus a sample is evidence, not proof -- compensating errors are unlikely, not
excluded. A mandate of "identical" is not met by a sample.

HOW EVERY NESTED VALUE IS COMPARED WITHOUT LOADING 6.8M ROWS. For each
document key, Postgres builds one deterministic SIGNATURE string from all of
that key's child rows, sorted. Python builds the same signature from the
document. Comparing the two strings compares every nested field of every
indication and every code -- exhaustively, with memory bounded to one
signature at a time.

WHAT COUNTS AS A DISCREPANCY.
  a document present in one side and not the other
  any scalar field differing in value
  a field NULL in Postgres but PRESENT in the document (the omission rule
    running backwards would be a silent invention)
  any difference at all in the nested signature

Every discrepancy is printed with its key and field. The run ends with a
count, and the count is the answer to "is it identical" -- not a sample.
"""
import os
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
OUT = r"C:\CHATHealthyLLC\DataExperiments\verify_exhaustive_result.txt"
TARGET_DB = "PipelinePublicHealthData"
MAX_REPORT = 40


def emit(m=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(m + "\n")
    _log.info("%s", m)


def pg():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def norm(v):
    """One canonical rendering, so Postgres numeric and BSON Decimal128 of the
    same number compare equal. Normalising the RENDERING is not normalising
    the VALUE -- a different number still differs."""
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


def doc_signature(doc):
    """The document's nested content, rendered exactly as Postgres renders it
    in sig_sql below. Any difference in any nested field changes the string."""
    parts = []
    for ind in doc.get("indications", []):
        for code in ind.get("codes", []):
            parts.append("|".join([
                norm(ind.get("indication_id")) or "",
                norm(ind.get("indication_text")) or "",
                norm(ind.get("corroborated_by_trial")) or "",
                norm(code.get("icd10_code")) or "",
                norm(code.get("who_level")) or "",
                norm(code.get("is_billable")) or "",
                norm(code.get("route")) or "",
                norm(code.get("ccsr_category")) or "",
                norm(code.get("body_system")) or "",
                norm(code.get("provenance")) or "",
                norm(code.get("confidence")) or "",
            ]))
    return "\n".join(sorted(parts))


# The same rendering, built by Postgres. trim_scale + a plain cast matches
# Decimal.normalize(); booleans render t/f, which norm() mirrors.
SIG_SQL = """
SELECT npi, molecule_id,
       string_agg(
         concat_ws('|',
           indication_id, indication_text, corroborated_by_trial::text,
           icd10_code, who_level::text, is_billable::text, route,
           coalesce(ccsr_category,''), coalesce(body_system,''),
           provenance, trim_scale(confidence)::text),
         E'\\n' ORDER BY
           concat_ws('|',
             indication_id, indication_text, corroborated_by_trial::text,
             icd10_code, who_level::text, is_billable::text, route,
             coalesce(ccsr_category,''), coalesce(body_system,''),
             provenance, trim_scale(confidence)::text)
       ) AS sig
FROM pt_providermedicare_indication
GROUP BY 1,2
"""


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("EXHAUSTIVE VERIFICATION -- every document, every field, no sampling")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()

    mc = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyDataPipelines")
    db = mc[TARGET_DB]
    con = pg(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='7200s'")

    bad = 0
    shown = 0

    def report(msg):
        nonlocal bad, shown
        bad += 1
        if shown < MAX_REPORT:
            shown += 1
            emit("   " + msg)

    # ---------------------------------------------- NUCCSpecialty, all of it
    emit("=== NUCCSpecialty -- all rows ===")
    cur.execute("SELECT * FROM pt_nuccspecialty")
    cols = [d[0] for d in cur.description]
    src = {}
    for row in cur.fetchall():
        r = dict(zip(cols, row))
        src[r["nucc_code"]] = r
    coll = db["NUCCSpecialty"]
    seen = 0
    for doc in coll.find({}):
        seen += 1
        k = doc.get("_id")
        s = src.pop(k, None)
        if s is None:
            report("NUCC %s in mongo, not in pg" % k); continue
        for f, v in s.items():
            got = doc.get(f)
            if v is None:
                if f in doc:
                    report("NUCC %s field %s NULL in pg, present in mongo" % (k, f))
                continue
            if norm(got) != norm(v):
                report("NUCC %s field %s pg=%r mongo=%r" % (k, f, v, got))
    for k in src:
        report("NUCC %s in pg, not in mongo" % k)
    emit("   documents compared: %s   discrepancies so far: %s"
         % (f"{seen:,}", f"{bad:,}"))

    # -------------------------------------- ProviderMedicare, parents, all
    emit()
    emit("=== ProviderMedicare -- every parent field of every document ===")
    cur.execute("SELECT * FROM pt_providermedicare")
    pcols = [d[0] for d in cur.description]
    parents = {}
    for row in cur.fetchall():
        r = dict(zip(pcols, row))
        parents[(r["npi"], r["molecule_id"])] = r
    emit("   pg rows: %s" % f"{len(parents):,}")

    coll = db["ProviderMedicare"]
    emit("   mongo documents: %s" % f"{coll.count_documents({}):,}")
    checked = 0
    t0 = time.time()
    mongo_keys = set()
    for doc in coll.find({}, batch_size=5000):
        checked += 1
        key = (doc.get("npi"), doc.get("molecule_id"))
        mongo_keys.add(key)
        s = parents.get(key)
        if s is None:
            report("doc %s|%s in mongo, no pg row" % key); continue
        for f, v in s.items():
            got = doc.get(f)
            if v is None:
                if f in doc:
                    report("%s|%s field %s NULL in pg, present in mongo"
                           % (key[0], key[1], f))
                continue
            if norm(got) != norm(v):
                report("%s|%s field %s pg=%r mongo=%r"
                       % (key[0], key[1], f, v, got))
        if checked % 100000 == 0:
            emit("      %s documents, %.0fs, %s discrepancies"
                 % (f"{checked:,}", time.time()-t0, f"{bad:,}"))
    for key in parents:
        if key not in mongo_keys:
            report("doc %s|%s in pg, not in mongo" % key)
    emit("   parent fields compared on %s documents" % f"{checked:,}")

    # ------------------------- ProviderMedicare, every nested value, all
    emit()
    emit("=== every NESTED value, by signature, on every document ===")
    sig_cur = con.cursor(name="sig_stream")
    sig_cur.itersize = 5000
    sig_cur.execute(SIG_SQL)
    n_sig = 0
    t0 = time.time()
    pg_keys = set()
    for npi, mol, sig in sig_cur:
        n_sig += 1
        pg_keys.add((npi, mol))
        doc = coll.find_one({"_id": "%s|%s" % (npi, mol)})
        if doc is None:
            report("child rows for %s|%s but no document" % (npi, mol)); continue
        got = doc_signature(doc)
        if got != (sig or ""):
            report("%s|%s NESTED CONTENT DIFFERS" % (npi, mol))
            if shown <= MAX_REPORT:
                a = (sig or "").split("\n"); b = got.split("\n")
                emit("        pg rows=%d mongo rows=%d" % (len(a), len(b)))
                for x in sorted(set(a) - set(b))[:2]:
                    emit("        only in pg    : %s" % x[:150])
                for x in sorted(set(b) - set(a))[:2]:
                    emit("        only in mongo : %s" % x[:150])
        if n_sig % 50000 == 0:
            emit("      %s signatures, %.0fs, %s discrepancies"
                 % (f"{n_sig:,}", time.time()-t0, f"{bad:,}"))
    sig_cur.close()
    emit("   nested signatures compared: %s" % f"{n_sig:,}")

    # a document carrying indications that pg has no child rows for
    extra = 0
    for doc in coll.find({"indications": {"$exists": True}},
                         {"npi": 1, "molecule_id": 1}, batch_size=5000):
        if (doc["npi"], doc["molecule_id"]) not in pg_keys:
            extra += 1
            report("doc %s|%s has indications, pg has no child rows"
                   % (doc["npi"], doc["molecule_id"]))
    emit("   documents with indications but no pg child rows: %s" % f"{extra:,}")

    emit()
    if bad == 0:
        emit("RESULT: IDENTICAL. Every document, every field, every nested")
        emit("value. %s provider documents and %s specialty documents compared"
             % (f"{checked:,}", f"{seen:,}"), )
        emit("with zero discrepancies. This is exhaustive, not sampled.")
    else:
        emit("RESULT: %s DISCREPANCIES. The data is NOT identical." % f"{bad:,}")
        if bad > MAX_REPORT:
            emit("(%s shown above, %s suppressed)" % (MAX_REPORT, bad-MAX_REPORT))
    con.close(); mc.close()
    emit()
    emit("written to " + OUT)
    raise ChatHealthyException(
        "runtime_error", "run stopped")


main()
