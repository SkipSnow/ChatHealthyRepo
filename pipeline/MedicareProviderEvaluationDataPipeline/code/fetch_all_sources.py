"""Fetch every raw source the chain needs. Idempotent, verifiable, complete.

WHY THIS FILE EXISTS. The loaders disagreed about fetching. pg_load_02 and
pg_load_04 pull from Azure; pg_load_05 pulls from ClinicalTrials.gov;
exec_01 (Medicare Part D, the largest single source) had no fetch at all and
only read a path. A process where one source cannot be re-fetched is not an
end-to-end process -- deleting the cache would have made it unrebuildable.

So the GUARANTEE lives here, once, for all nine sources, instead of being
scattered across loaders that each made their own assumption.

WHERE EACH SOURCE COMES FROM.
  Azure, stchpipelinedev/dev-pipeline-sources -- medicare_partd,
    openfda_labels (42 shards), rxnorm, rxnorm_rel, icd10_cm, ccsr,
    medicare_coverage_icd10, medicare_coverage_hcpc
  ClinicalTrials.gov api/v2 -- the trials corpus, which was never archived to
    Azure. That is a real gap in the archive and it is recorded rather than
    hidden; the fetch goes to the public source.

WHAT "VERIFIED" MEANS HERE. A download is not trusted because it completed.
Every file is checked for non-zero size, and every file that should be a zip
is opened as one and its entry count read. A truncated download that happened
to finish would otherwise pass.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
import zipfile

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

SOURCES = r"C:\DataExperiments\Sources"
OUT = r"C:\CHATHealthyLLC\pipeline\MedicareProviderEvaluationDataPipeline\code\fetch_all_sources_result.txt"
ACCOUNT = "stchpipelinedev"
CONTAINER = "dev-pipeline-sources"

# The Azure prefixes the chain consumes. NO SIZE FLOORS. Each prefix used to
# carry a "minimum bytes a healthy copy has" -- 3 GB for Part D, 100 MB for
# openFDA and so on. Every one of those numbers was a figure I observed once
# and then enforced on every future run. They cannot detect a bad download,
# because the real check is the one Azure makes possible: the downloaded file
# must be EXACTLY the byte count Azure states for that blob, which is a fact
# rather than a guess, and it is enforced per blob below. A floor only adds a
# second opinion that is wrong whenever the corpus legitimately changes.
AZURE_PREFIXES = (
    "medicare_partd",
    "openfda_labels",
    "rxnorm",
    "rxnorm_rel",
    "icd10_cm",
    "ccsr",
    "medicare_coverage_icd10",
    "medicare_coverage_hcpc",
)
CT_DIR = os.path.join(SOURCES, "clinicaltrials")
CT_ZIP = os.path.join(CT_DIR, "ctg-studies.json.zip")
CT_URL = "https://clinicaltrials.gov/api/v2/studies/download?format=json.zip"


def emit(m=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(m + "\n")
    _log.info("%s", m)


def die(detail):
    emit()
    emit("FETCH FAILED: %s" % detail)
    emit("the rebuild cannot proceed without every source present.")
    raise ChatHealthyException(
        "runtime_error", "fetch failed: %s" % detail)


def az():
    for c in (r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
              r"C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"):
        if os.path.exists(c):
            return c
    return "az"


AZ = az()


def blob_list():
    r = subprocess.run([AZ, "storage", "blob", "list",
                        "--account-name", ACCOUNT,
                        "--container-name", CONTAINER,
                        "--auth-mode", "login",
                        "--query", "[].{n:name,s:properties.contentLength}",
                        "-o", "json"],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        die("blob list failed: " + (r.stderr or "")[:400])
    return json.loads(r.stdout)


def download(name, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    r = subprocess.run([AZ, "storage", "blob", "download",
                        "--account-name", ACCOUNT,
                        "--container-name", CONTAINER,
                        "--name", name, "--file", dest,
                        "--auth-mode", "login", "--no-progress",
                        "-o", "none"],
                       capture_output=True, text=True, timeout=7200)
    if r.returncode != 0:
        die("download of %s failed: %s" % (name, (r.stderr or "")[:400]))


def verify(path, label):
    """Non-zero, and a readable archive if it claims to be one. A download
    that merely finished is not a download that is intact."""
    if not os.path.exists(path):
        die("%s missing after fetch: %s" % (label, path))
    sz = os.path.getsize(path)
    if sz == 0:
        die("%s is zero bytes: %s" % (label, path))
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as z:
                n = len(z.namelist())
            return sz, "zip, %s entries" % f"{n:,}"
        except Exception as exc:
            die("%s is a corrupt zip: %s (%s)" % (label, path, exc))
    return sz, "plain"


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    # Optional subset on argv: a collection that does not read a source has no
    # business downloading it. The FDA collection needs openFDA and RxNorm and
    # nothing else; bare invocation still fetches everything.
    wanted = [a for a in sys.argv[1:] if not a.startswith("-")]
    want_ct = (not wanted) or ("clinicaltrials" in wanted)
    prefixes = [p for p in AZURE_PREFIXES if (not wanted) or p in wanted]
    if wanted:
        missing = [w for w in wanted
                   if w not in AZURE_PREFIXES and w != "clinicaltrials"]
        if missing:
            die("unknown source(s): %s" % ", ".join(missing))
    emit("FETCH RAW SOURCES")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("destination: %s" % SOURCES)
    emit("requested: %s" % (", ".join(wanted) if wanted else "every source"))
    emit()
    os.makedirs(SOURCES, exist_ok=True)
    t0 = time.time()

    emit("=== Azure: %s/%s ===" % (ACCOUNT, CONTAINER))
    blobs = blob_list()
    emit("   %d blobs in the container" % len(blobs))
    total_bytes = 0
    # THE MANIFEST. Each source's version is the sorted blob names it was
    # built from plus their exact byte sizes, hashed. That is a fingerprint of
    # the archived snapshot: if Azure serves a different or newer file the
    # hash changes, so a document stamped with the old hash can never be
    # mistaken for one built from the new data. The delivery refuses to write
    # a collection whose source versions it cannot name.
    manifest = {"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "account": ACCOUNT, "container": CONTAINER, "sources": {}}
    for prefix in sorted(prefixes):
        mine = [b for b in blobs if b["n"].startswith(prefix + "/")]
        if not mine:
            die("no blob under prefix '%s/' -- the chain needs it and the "
                "archive does not have it" % prefix)
        fingerprint = hashlib.sha256(
            "\n".join("%s:%s" % (b["n"], b.get("s") or 0)
                      for b in sorted(mine, key=lambda x: x["n"]))
            .encode("utf-8")).hexdigest()[:16]
        manifest["sources"][prefix] = "%s@%s" % (prefix, fingerprint)
        got = 0
        for b in mine:
            dest = os.path.join(SOURCES, *b["n"].split("/"))
            want = int(b.get("s") or 0)
            if os.path.exists(dest) and os.path.getsize(dest) == want and want:
                got += os.path.getsize(dest)
                continue
            download(b["n"], dest)
            sz, kind = verify(dest, b["n"])
            if want and sz != want:
                die("%s is %d bytes, Azure says %d -- truncated"
                    % (b["n"], sz, want))
            got += sz
        total_bytes += got
        emit("   %-26s %3d blob(s)  %8.0f MB  ok  %s"
             % (prefix, len(mine), got/1e6, manifest["sources"][prefix]))

    man_path = os.path.join(SOURCES, "fetch_manifest.json")
    with open(man_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    emit()
    emit("   source versions recorded in %s" % man_path)

    emit()
    if not want_ct:
        emit("=== ClinicalTrials.gov skipped (not requested) ===")
        return
    emit("=== ClinicalTrials.gov (not in the Azure archive) ===")
    os.makedirs(CT_DIR, exist_ok=True)
    # Re-download unless a readable zip is already here. The old test was
    # "bigger than 1 GB and more than 100,000 entries" -- two numbers I chose.
    # Whether the archive OPENS is a fact; how big it ought to be is not.
    need = True
    if os.path.exists(CT_ZIP):
        try:
            with zipfile.ZipFile(CT_ZIP) as z:
                need = not z.namelist()
        except Exception:
            need = True
    if need:
        emit("   downloading %s ..." % CT_URL)
        t = time.time()
        req = urllib.request.Request(CT_URL, headers={"User-Agent": "Mozilla/5.0"})
        tmp = CT_ZIP + ".part"
        with urllib.request.urlopen(req, timeout=7200) as src, open(tmp, "wb") as dst:
            while True:
                chunk = src.read(1 << 22)
                if not chunk:
                    break
                dst.write(chunk)
        os.replace(tmp, CT_ZIP)
        emit("   downloaded %.0f MB in %.0fs"
             % (os.path.getsize(CT_ZIP)/1e6, time.time()-t))
    sz, kind = verify(CT_ZIP, "clinicaltrials")
    total_bytes += sz
    emit("   %-26s %8.0f MB  %s" % ("clinicaltrials", sz/1e6, kind))

    emit()
    emit("=== EVERY REQUESTED SOURCE PRESENT ===")
    emit("   %.1f GB across %d source sets in %.0f minutes"
         % (total_bytes/1e9, len(prefixes) + (1 if want_ct else 0),
            (time.time()-t0)/60))
    emit()
    emit("   recorded gap: the clinical-trials corpus is NOT archived in")
    emit("   Azure. It is fetched from the public source, so the rebuild")
    emit("   depends on ClinicalTrials.gov being reachable. Archiving it")
    emit("   would remove that dependency and is a write to a dev source")
    emit("   container, which is the operator's to authorise.")
    emit()
    emit("written to " + OUT)


main()
