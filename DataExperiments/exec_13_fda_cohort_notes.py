"""Execution step 13 -- keep the cohort notes the FDA publishes.

THE FINDING THIS ANSWERS. The openFDA label record carries 122 top-level
fields. Our Postgres load kept 12 and discarded 110, among them every section
that says WHICH POPULATION a drug is for. That is what was being dropped on
the floor:

    contraindications              35.5% of labels
    pediatric_use                  28.8%
    pregnancy                      28.6%
    geriatric_use                  25.3%
    use_in_specific_populations    21.1%
    pregnancy_or_breast_feeding    18.6%
    nursing_mothers                15.5%
    boxed_warning                  13.5%

ADDITIVE BY DESIGN. This does NOT re-load fda_label and does not touch
mol_label, T1, T3, T4 or T5. Rewriting fda_label would invalidate the whole
measured chain to add columns nothing downstream reads yet. Instead the cohort
notes land in their own table keyed by set_id, so they join when a requirement
asks for them and change nothing until then.

WHAT THIS IS NOT. It stores the label's own prose verbatim. It does NOT decide
whether a drug is approved for children -- that reading is an inference and
belongs to a route that is declared as one. Storing the sentence is a fact;
interpreting it is not, and conflating them here would bury an unapproved rule
in the data.

A NOTE ON SEX. There is no sex or gender field in the record. Sex-specific
restrictions live inside the prose of use_in_specific_populations and
contraindications. So age arrives as a section that can be read directly,
while sex requires interpreting narrative. The two are not symmetric and this
table does not pretend they are.
"""
import sys
import collections
import io
import json
import os
import time
import zipfile

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\exec_13_cohort_result.txt"
SHARD_DIR = r"C:\DataExperiments\Sources\openfda_labels"

# The cohort notes: the sections that state which population a drug is for,
# or must not be given to. Named explicitly rather than pattern-matched, so
# the set is reviewable and a future FDA field cannot silently join it.
COHORT_FIELDS = [
    "pediatric_use",
    "geriatric_use",
    "pregnancy",
    "pregnancy_or_breast_feeding",
    "nursing_mothers",
    "use_in_specific_populations",
    "labor_and_delivery",
    "contraindications",
    "boxed_warning",
]
# The _table variants carry the same content as rendered tables. Kept
# separately rather than concatenated, because merging prose with table markup
# would make the prose unquotable -- and quoting the label to the user is the
# whole point of holding it.
TABLE_FIELDS = [f + "_table" for f in COHORT_FIELDS]


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def shards():
    out = []
    for root, _, files in os.walk(SHARD_DIR):
        for f in files:
            p = os.path.join(root, f)
            if zipfile.is_zipfile(p):
                out.append(p)
    return sorted(out)


def records(path):
    """Stream the openFDA bulk file: a zip holding one JSON object whose
    'results' key is the record array. Streamed rather than loaded, because
    the decompressed shard is far larger than the archive."""
    z = zipfile.ZipFile(path)
    name = z.namelist()[0]
    dec = json.JSONDecoder()
    with z.open(name) as fh:
        txt = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
        buf = ""
        while '"results"' not in buf:
            chunk = txt.read(65536)
            if not chunk:
                return
            buf += chunk
        i = buf.index('"results"')
        buf = buf[buf.index('[', i) + 1:]
        while True:
            buf = buf.lstrip().lstrip(',').lstrip()
            if not buf or buf.startswith(']'):
                return
            if not buf.startswith('{'):
                more = txt.read(262144)
                if not more:
                    return
                buf += more
                continue
            try:
                obj, end = dec.raw_decode(buf)
            except ValueError:
                more = txt.read(262144)
                if not more:
                    return
                buf += more
                continue
            yield obj
            buf = buf[end:]


def flat(v):
    """openFDA gives every narrative section as a list of strings."""
    if v is None:
        return None
    if isinstance(v, list):
        s = "\n".join(str(x) for x in v if x is not None)
    else:
        s = str(v)
    s = s.strip()
    return s or None


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("EXECUTION STEP 13 -- keep the FDA cohort notes")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("additive: fda_label is NOT reloaded and the measured chain is "
         "untouched")
    emit()

    files = shards()
    emit("shards found: %d" % len(files))
    if not files:
        raise ChatHealthyException("runtime_error", "no shards under " + SHARD_DIR)

    con = connect(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='7200s'")

    cols = ["set_id text", "spl_id text", "effective_time text"]
    cols += ["%s text" % f for f in COHORT_FIELDS]
    cols += ["%s text" % f for f in TABLE_FIELDS]
    cur.execute("DROP TABLE IF EXISTS fda_label_cohort")
    cur.execute("CREATE TABLE fda_label_cohort (%s)" % ", ".join(cols))

    ncols = 3 + len(COHORT_FIELDS) + len(TABLE_FIELDS)
    placeholders = "(" + ",".join(["%s"] * ncols) + ")"
    present = collections.Counter()
    seen = 0
    t0 = time.time()
    batch = []

    def flush():
        if not batch:
            return
        args = b",".join(cur.mogrify(placeholders, r) for r in batch)
        cur.execute(b"INSERT INTO fda_label_cohort VALUES " + args)
        batch.clear()

    for idx, path in enumerate(files, 1):
        for rec in records(path):
            seen += 1
            row = [flat(rec.get("set_id")), flat(rec.get("id")),
                   flat(rec.get("effective_time"))]
            any_cohort = False
            for f in COHORT_FIELDS + TABLE_FIELDS:
                v = flat(rec.get(f))
                if v is not None:
                    present[f] += 1
                    any_cohort = True
                row.append(v)
            if any_cohort:
                batch.append(tuple(row))
                if len(batch) >= 2000:
                    flush()
        flush()
        emit("  shard %2d/%d  labels read=%s  rows kept so far  %.0fs"
             % (idx, len(files), f"{seen:,}", time.time() - t0))

    flush()
    cur.execute("CREATE INDEX flc_set ON fda_label_cohort (set_id)")
    cur.execute("ANALYZE fda_label_cohort")

    emit()
    emit("=== WHAT WAS KEPT ===")
    cur.execute("SELECT count(*), count(DISTINCT set_id) FROM fda_label_cohort")
    n, ns = cur.fetchone()
    emit("  labels read from the shards : %s" % f"{seen:,}")
    emit("  rows with any cohort note   : %s  (%s distinct set_id)"
         % (f"{n:,}", f"{ns:,}"))
    emit()
    emit("  %-34s %10s %9s" % ("field", "labels", "share"))
    for f in COHORT_FIELDS + TABLE_FIELDS:
        c = present.get(f, 0)
        if c:
            emit("  %-34s %10s %8.1f%%"
                 % (f, f"{c:,}", 100.0 * c / max(seen, 1)))

    emit()
    emit("=== REACH INTO THE CHAIN -- molecules that gain a cohort note ===")
    cur.execute("""
        SELECT count(DISTINCT ml.molecule_id)
        FROM mol_label ml JOIN fda_label_cohort c ON c.set_id = ml.set_id""")
    withc = cur.fetchone()[0]
    cur.execute("SELECT count(DISTINCT molecule_id) FROM mol_label")
    tot = cur.fetchone()[0]
    emit("  molecules with at least one cohort note: %s of %s (%.1f%%)"
         % (f"{withc:,}", f"{tot:,}", 100.0 * withc / max(tot, 1)))
    cur.execute("""
        SELECT count(DISTINCT ml.molecule_id)
        FROM mol_label ml JOIN fda_label_cohort c ON c.set_id = ml.set_id
        WHERE c.pediatric_use IS NOT NULL""")
    emit("  molecules with a PEDIATRIC_USE section : %s"
         % f"{cur.fetchone()[0]:,}")

    emit()
    emit("=== A SAMPLE, VERBATIM -- what we can quote to a user ===")
    cur.execute("""SELECT set_id, left(pediatric_use, 300)
                   FROM fda_label_cohort
                   WHERE pediatric_use IS NOT NULL
                     AND length(pediatric_use) BETWEEN 80 AND 400
                   LIMIT 3""")
    for sid, txt in cur.fetchall():
        emit("  %s" % sid)
        emit("     %s" % txt.replace("\n", " ")[:280])
    con.close()
    emit()
    emit("written to " + OUT)


main()
