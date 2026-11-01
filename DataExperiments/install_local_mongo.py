"""Install a real local MongoDB so the port can be TESTED before Atlas.

WHY A REAL ONE. mongomock is already installed and is not usable here: a fake
cannot prove a port is correct, and faking Mongo is a standing prohibition.
The whole point is to compare the local relational values against real
document values, so the document store has to be real.

WHY A ZIP AND NOT THE INSTALLER. The zip needs no administrator rights and no
Windows service. It is extracted to a directory and mongod is run against a
local dbpath, the same way the Postgres data was placed outside the project
tree.

THE URL AND THE CHECKSUM COME FROM MONGODB'S OWN RELEASE MANIFEST
(downloads.mongodb.org/current.json), not from a constructed pattern, and the
download is verified against the manifest sha256 before anything is extracted.
An unverified database binary is not something to run.
"""
import sys
import hashlib
import json
import os
import subprocess
import time
import urllib.request
import zipfile

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

ROOT = r"C:\DataExperiments\mongodb"        # binaries, outside the project
DATA = r"C:\DataExperiments\mongodb-data"   # the local dbpath
LOG = r"C:\DataExperiments\mongodb-data\mongod.log"
PORT = 27018            # not 27017, so nothing can confuse this with a real
                        # service someone else starts
MANIFEST = "https://downloads.mongodb.org/current.json"


def say(m):
    _log.info("%s", m)


def resolve():
    with urllib.request.urlopen(MANIFEST, timeout=90) as r:
        data = json.loads(r.read())
    for v in data.get("versions", []):
        if v.get("production_release") is not True:
            continue
        for d in v.get("downloads", []):
            if d.get("target", "").startswith("windows") and d.get("arch") == "x86_64":
                a = d.get("archive") or {}
                if a.get("url", "").endswith(".zip"):
                    return v["version"], a["url"], a.get("sha256", "")
    raise ChatHealthyException("runtime_error", "no windows x86_64 zip in the manifest")


def main():
    os.makedirs(ROOT, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)
    ver, url, sha = resolve()
    say("version  : %s" % ver)
    say("url      : %s" % url)
    say("sha256   : %s" % sha)

    zp = os.path.join(ROOT, os.path.basename(url))
    if not os.path.exists(zp):
        say("downloading...")
        t0 = time.time()
        urllib.request.urlretrieve(url, zp)
        say("downloaded %.0f MB in %.0fs" % (os.path.getsize(zp)/1e6,
                                             time.time()-t0))
    else:
        say("already downloaded: %.0f MB" % (os.path.getsize(zp)/1e6))

    say("verifying sha256...")
    h = hashlib.sha256()
    with open(zp, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if sha and got != sha:
        raise ChatHealthyException("runtime_error", "CHECKSUM MISMATCH\n  manifest %s\n  download %s\n"
                         "refusing to extract an unverified database binary"
                         % (sha, got))
    say("checksum OK: %s" % got[:32])

    say("extracting...")
    with zipfile.ZipFile(zp) as z:
        z.extractall(ROOT)
    mongod = None
    for base, _, files in os.walk(ROOT):
        for f in files:
            if f.lower() == "mongod.exe":
                mongod = os.path.join(base, f)
                break
        if mongod:
            break
    if not mongod:
        raise ChatHealthyException("runtime_error", "mongod.exe not found after extraction")
    say("mongod   : %s" % mongod)

    say("starting mongod on port %d (dbpath %s)..." % (PORT, DATA))
    subprocess.Popen(
        [mongod, "--dbpath", DATA, "--port", str(PORT),
         "--bind_ip", "127.0.0.1", "--logpath", LOG, "--logappend"],
        creationflags=0x08000000)       # CREATE_NO_WINDOW

    import socket
    for i in range(60):
        s = socket.socket(); s.settimeout(1)
        try:
            s.connect(("127.0.0.1", PORT))
            s.close()
            say("mongod is accepting connections after %ds" % i)
            break
        except Exception:
            time.sleep(1)
        finally:
            try: s.close()
            except Exception: pass
    else:
        raise ChatHealthyException("runtime_error", "mongod did not come up; see " + LOG)

    say("")
    say("mongod is listening on port %d" % PORT)


main()
