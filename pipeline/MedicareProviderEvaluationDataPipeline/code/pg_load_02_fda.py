"""Stage 2 -- load every NON-OTC openFDA label record.

Source is AZURE, not the internet: the archived source blobs in
dev-pipeline-sources, which are the exact bytes the pipeline staged.

No records are chosen and none are capped. Blobs are streamed in sorted name
order and every non-OTC record is taken in the order it is encountered until
the blobs are exhausted. Each row carries the blob it came from,
so if the stream wraps into another run's copy of the same corpus the
repeated set_ids are visible in the data instead of being deduped away.

OTC exclusion uses the pipeline's OWN committed _otc_basis, and every
exclusion is recorded with its reason -- an excluded record must be
countable, not an absence.

Flat and relational. load_seq is the sequence of the load and nothing else.
"""
import csv as _csv
import io
import json
import os
import subprocess
import sys
import time
import zipfile

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC")
sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

import ijson  # noqa: E402

from pipeline.MedicareProviderEvaluationDataPipeline.steps.index_fda_prescription_records import (  # noqa: E402
    _has_indication, _otc_basis)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (  # noqa: E402
    brand_join_key, molecule_canonical)

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
DB = "medicare_join"
LOG = r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline\code\pg_load_progress.txt"
SOURCES = r"C:\DataExperiments\Sources"
ACCOUNT = "stchpipelinedev"
CONTAINER = "dev-pipeline-sources"

NARRATIVE = ("description", "spl_product_data_elements", "active_ingredient",
             "indications_and_usage", "package_label_principal_display_panel",
             "spl_unclassified_section", "how_supplied")


def note(msg):
    line = time.strftime("%H:%M:%S ") + msg
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    _log.info("%s", line)


def _az_path():
    """az is a .cmd shim on Windows; subprocess will not find a bare 'az'."""
    import shutil
    for cand in ("az", "az.cmd", "az.bat"):
        found = shutil.which(cand)
        if found:
            return found
    for cand in (r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
                 r"C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"):
        if os.path.exists(cand):
            return cand
    raise ChatHealthyException("runtime_error", "cannot locate the az CLI")


AZ = _az_path()


def connect():
    with open(PW_FILE, encoding="utf-8") as fh:
        pw = fh.read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname=DB)


def schema(con):
    with con.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS fda_name")
        cur.execute("DROP TABLE IF EXISTS fda_label")
        cur.execute("DROP TABLE IF EXISTS fda_otc_excluded")
        cur.execute("""
            CREATE TABLE fda_label (
              load_seq       bigserial PRIMARY KEY,
              source_blob    text,
              set_id         text,
              spl_id         text,
              effective_time text,
              version        text,
              product_type   text,
              -- the openfda block's own identifiers. These were in the file
              -- all along and were simply never extracted, which is why
              -- drug_class, unii and substance_name read as unfillable.
              unii           text,
              substance_name text,
              pharm_class    text,
              fda_rxcui      text,
              has_indication boolean,
              """ + ",\n              ".join(f"{c} text" for c in NARRATIVE) + """
            )""")
        cur.execute("""
            CREATE TABLE fda_name (
              load_seq   bigserial PRIMARY KEY,
              set_id     text,
              kind       text,
              value      text,
              value_norm text
            )""")
        cur.execute("""
            CREATE TABLE fda_otc_excluded (
              load_seq    bigserial PRIMARY KEY,
              source_blob text,
              set_id      text,
              otc_basis   text
            )""")
        cur.execute("DROP TABLE IF EXISTS fda_otc_name")
    con.commit()
    note("created fda_label, fda_name, fda_otc_excluded")


def blob_names():
    """The blobs of the ONE snapshot the fetch chose, read from its manifest.

    This used to list openfda_labels/ against Azure and take every name it got
    back. The archive keeps every fetch it has ever made under its own hash
    directory, so that took three snapshots thirteen days apart: 42 blobs, and
    fda_label held 281,838 rows over 93,956 distinct set_ids -- 93,933 of them
    appearing exactly three times, one spl_id and one version each. Three
    copies of the same label, and which copy won a downstream read depended on
    load order.

    The choice belongs to fetch_all_sources, which already makes it and logs
    it. Reading the manifest means the rule lives in one place; repeating the
    newest-snapshot logic here is what let the two drift apart in the first
    place. No fallback: if the manifest is missing or does not name the
    snapshot, the fetch did not run and this load has no business guessing.
    """
    man_path = os.path.join(SOURCES, "fetch_manifest.json")
    if not os.path.isfile(man_path):
        raise ChatHealthyException(
            "config_error",
            "no fetch manifest at %s -- run fetch_all_sources.py first" % man_path)
    with open(man_path, encoding="utf-8") as fh:
        man = json.load(fh)
    names = sorted((man.get("blobs") or {}).get("openfda_labels") or [])
    if not names:
        raise ChatHealthyException(
            "config_error",
            "fetch manifest names no openfda_labels blobs; it was written by a "
            "fetch that predates snapshot selection -- re-run fetch_all_sources.py")
    note("openfda snapshot %s: %d blob(s)"
         % ((man.get("snapshots") or {}).get("openfda_labels", "?"), len(names)))
    return names


def local_path(name):
    """Cache each blob under Sources at its own blob path, so a later pass
    over the corpus reads from disk instead of pulling 5.5 GB again."""
    return os.path.join(SOURCES, name.replace("/", os.sep))


def download(name):
    dest = local_path(name)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        note("  cached %s" % dest)
        return dest
    out = subprocess.run(
        [AZ, "storage", "blob", "download", "--account-name", ACCOUNT,
         "--container-name", CONTAINER, "--auth-mode", "login",
         "--name", name, "--file", dest, "--no-progress", "-o", "none"],
        capture_output=True, text=True, timeout=3600)
    if out.returncode != 0:
        raise ChatHealthyException("runtime_error", "download failed for %s: %s" % (name, out.stderr[:500]))
    return dest


def _text(v):
    if isinstance(v, list):
        return "\n".join(str(x) for x in v if x)
    if isinstance(v, dict):
        return json.dumps(v)
    return "" if v is None else str(v)


def _names(v):
    if isinstance(v, str):
        v = [v]
    return [str(x).strip() for x in (v or []) if str(x or "").strip()]


def main():
    con = connect()
    schema(con)
    cur = con.cursor()

    kept = otc = seen = 0
    seen_setids = set()
    repeated = 0

    for blob in blob_names():
        path = download(blob)
        note("reading %s (%.1f MB)  kept_so_far=%d"
             % (blob, os.path.getsize(path) / 1e6, kept))

        lbuf, nbuf, obuf = io.StringIO(), io.StringIO(), io.StringIO()
        lw, nw, ow = _csv.writer(lbuf), _csv.writer(nbuf), _csv.writer(obuf)
        before = kept

        with zipfile.ZipFile(path) as zf:
            for member_name in zf.namelist():
                with zf.open(member_name) as member:
                    for raw in ijson.items(member, "results.item", use_float=True):
                        seen += 1
                        sid = raw.get("set_id")
                        basis = _otc_basis(raw)
                        if basis is not None:
                            otc += 1
                            ow.writerow([blob, sid, basis])
                            continue
                        if sid in seen_setids:
                            repeated += 1
                        else:
                            seen_setids.add(sid)
                        kept += 1
                        ofda = raw.get("openfda") or {}
                        # pharm_class is four parallel arrays in the source --
                        # established pharmacologic class, mechanism of
                        # action, chemical structure, physiologic effect. All
                        # four are the drug's classification; they are kept
                        # together rather than one picked over the others.
                        pharm = []
                        for pc in ("pharm_class_epc", "pharm_class_moa",
                                   "pharm_class_cs", "pharm_class_pe"):
                            pharm.extend(_names(ofda.get(pc)))
                        lw.writerow([
                            blob, sid, raw.get("id"), raw.get("effective_time"),
                            raw.get("version"), _text(ofda.get("product_type")),
                            _text(ofda.get("unii")),
                            _text(ofda.get("substance_name")),
                            "\n".join(pharm),
                            _text(ofda.get("rxcui")),
                            "true" if _has_indication(raw) else "false",
                        ] + [_text(raw.get(c)) for c in NARRATIVE])
                        for kind, field, norm in (
                            ("brand", "brand_name", brand_join_key),
                            ("generic", "generic_name", molecule_canonical),
                            ("substance", "substance_name", molecule_canonical),
                        ):
                            for v in _names(ofda.get(field)):
                                nw.writerow([sid, kind, v, norm(v) or ""])

        for buf, table, cols in (
            (lbuf, "fda_label",
             ["source_blob", "set_id", "spl_id", "effective_time", "version",
              "product_type", "unii", "substance_name", "pharm_class",
              "fda_rxcui", "has_indication"] + list(NARRATIVE)),
            (nbuf, "fda_name", ["set_id", "kind", "value", "value_norm"]),
            (obuf, "fda_otc_excluded", ["source_blob", "set_id", "otc_basis"]),
        ):
            buf.seek(0)
            cur.copy_expert("COPY %s (%s) FROM STDIN WITH (FORMAT csv)"
                            % (table, ",".join(cols)), buf)
        con.commit()
        note("  +%d kept (total %d), otc_excluded=%d, repeated_set_ids=%d"
             % (kept - before, kept, otc, repeated))

    note("indexing the join surface")
    for ddl in (
        "CREATE INDEX fda_name_value_upper ON fda_name (upper(value))",
        "CREATE INDEX fda_name_norm ON fda_name (value_norm)",
        "CREATE INDEX fda_name_kind_norm ON fda_name (kind, value_norm)",
        "CREATE INDEX fda_label_setid ON fda_label (set_id)",
    ):
        cur.execute(ddl)
    con.commit()

    cur.execute("SELECT count(*), count(DISTINCT set_id) FROM fda_label")
    nl, nd = cur.fetchone()
    cur.execute("SELECT count(*) FROM fda_name")
    nn = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM fda_otc_excluded")
    no = cur.fetchone()[0]
    note("RESULT fda_label rows=%d distinct_set_id=%d (repeats=%d)"
         % (nl, nd, nl - nd))
    note("       fda_name rows=%d   otc_excluded=%d   corpus_records_seen=%d"
         % (nn, no, seen))
    cur.execute("SELECT kind, count(*), count(DISTINCT value_norm) "
                "FROM fda_name GROUP BY kind ORDER BY kind")
    for kind, c, d in cur.fetchall():
        note("  %-10s rows=%-8d distinct_norm=%d" % (kind, c, d))
    cur.execute("SELECT otc_basis, count(*) FROM fda_otc_excluded "
                "GROUP BY otc_basis ORDER BY 2 DESC LIMIT 15")
    note("OTC exclusion reasons:")
    for b, c in cur.fetchall():
        note("  %-42s %d" % (b, c))
    cur.execute("SELECT count(*) FROM fda_label WHERE has_indication")
    note("of the loaded rows, states an indication: %d" % cur.fetchone()[0])
    con.close()
    note("STAGE 2 DONE")


main()
