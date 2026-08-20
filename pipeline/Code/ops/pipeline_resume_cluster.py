# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Resume the pipeline cluster, and keep asking until it serves.

The mirror of pipeline_pause_cluster.py. Atlas refuses a resume for reasons
that pass on their own -- a cluster still UPDATING from the pause, or resumed
so recently the request collides with itself -- so this does not give up
either. It asks until the cluster answers a ping, and then writes to it every
second until a write succeeds -- ping is documented as returning even when the
server is write-locked, so only a write that landed proves the cluster will
take the work. It gives up after ten minutes of refused writes. The status
label Atlas reports is logged and never used to decide.

    python pipeline/Code/ops/pipeline_resume_cluster.py
    python pipeline/Code/ops/pipeline_resume_cluster.py --cluster NAME
    python pipeline/Code/ops/pipeline_resume_cluster.py --interval 30

Exit: 0 the cluster is running. It does not exit otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request
from urllib.request import (HTTPDigestAuthHandler, HTTPPasswordMgrWithDefaultRealm,
                            build_opener)

_HERE = pathlib.Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / "pipeline" / "Code").is_dir():
        sys.path.insert(0, str(_parent / "pipeline" / "Code"))
        sys.path.insert(0, str(_parent / "ChatHealthyLib" / "src"))
        _ROOT = _parent
        break

_ENV = _ROOT / ".env"
if _ENV.is_file():
    for _line in _ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in _line and not _line.strip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

os.environ.setdefault("CH_SPACE_NAME", "pipeline-resume-cluster")
os.environ.setdefault("CH_COMPONENT", "pipeline-resume-cluster")
os.environ["CH_LOG_DESTINATION"] = "stdout"

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

log = ChatHealthyLoggingService()

# Waiting for the cluster to take a write, after it answers a ping.
_PING_SECONDS = 5
_RETRY_SECONDS = 1
_REPORT_SECONDS = 30
_DEADLINE_SECONDS = 600
_READINESS_DB = "pipelineAdmin"
_READINESS_COLLECTION = "cluster_readiness"
_READINESS_ID = "resume-readiness"

_ATLAS = "https://cloud.mongodb.com/api/atlas/v1.0"


def _raise_no_atlas_key() -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="atlas_credential_absent",
        component="pipeline_resume_cluster",
        message=("ATLAS_PIPELINE_PUBLIC_KEY / ATLAS_PIPELINE_PRIVATE_KEY and "
                 "ATLAS_PROJECT_ID must be present to resume a cluster."))


class AtlasCluster:
    """The one cluster this asks about."""

    def __init__(self, project: str, name: str, public: str, private: str) -> None:
        self._url = f"{_ATLAS}/groups/{project}/clusters/{name}"
        self._public = public
        self._private = private
        self.name = name

    def _open(self, method: str, body: dict | None = None):
        mgr = HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, self._url, self._public, self._private)
        opener = build_opener(HTTPDigestAuthHandler(mgr))
        req = urllib.request.Request(
            self._url,
            data=json.dumps(body).encode("utf-8") if body else None,
            method=method,
            headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def state(self) -> tuple[str, bool]:
        d = self._open("GET")
        return str(d.get("stateName")), bool(d.get("paused"))

    def resume(self) -> str:
        """Returns "" on acceptance, or Atlas's reason for refusing."""
        try:
            self._open("PATCH", {"paused": False})
            return ""
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                d = json.loads(raw)
                return f"{exc.code} {d.get('errorCode')}: {str(d.get('detail'))[:130]}"
            except ValueError:
                return f"{exc.code} {raw[:130]}"


def _answers(cluster_name: str, identity: str) -> bool:
    """True when the cluster answers a ping.

    Whether a database is up is not a question about a status label. Atlas
    reported REPAIRING while the cluster answered a connection in 25 seconds
    and listed its collections, and a wait keyed on the label IDLE held a
    wait against a database that was working. So this asks the database.
    """
    try:
        client = ChatHealthyMongoUtilities().getConnection(identity, cluster_name)
        client["admin"].command("ping")
        return True
    except Exception as exc:  # noqa: BLE001
        log.info("ping refused (%s: %s)", type(exc).__name__, str(exc)[:120])
        return False


def _raise_not_usable(cluster_name: str, pings: int, writes: int,
                      last: str) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="cluster_unavailable",
        component="pipeline_resume_cluster",
        message=(f"{cluster_name} was not usable within {_DEADLINE_SECONDS}s: "
                 f"{pings} ping(s), {writes} write attempt(s); "
                 f"last refusal {last or '<never reached a write>'}"),
        cluster_name=cluster_name)


def _wait_until_usable(cluster_name: str, identity: str) -> None:
    """Return when the cluster takes a write. Raise after ten minutes.

    An outer ping loop and an inner write loop. The ping is the cheap
    question -- is anything there -- asked every five seconds while the
    answer is no. Once it answers, that question is settled and is not asked
    again; the inner loop writes every second, because a ping is not
    evidence of a usable cluster: MongoDB documents it as a no-op that
    "will return immediately even if the server is write-locked".

    Both loops report every thirty seconds. The ten minutes is the whole
    wait, not each loop's.
    """
    began = time.monotonic()
    spoken = began
    pings = writes = 0
    last = ""

    def due() -> bool:
        nonlocal spoken
        if time.monotonic() - spoken < _REPORT_SECONDS:
            return False
        spoken = time.monotonic()
        return True

    while time.monotonic() - began < _DEADLINE_SECONDS:
        pings += 1
        if not _answers(cluster_name, identity):
            if due():
                log.info("still pinging %s after %.0fs, %d ping(s)",
                         cluster_name, time.monotonic() - began, pings)
            time.sleep(_PING_SECONDS)
            continue

        while time.monotonic() - began < _DEADLINE_SECONDS:
            writes += 1
            try:
                collection = (ChatHealthyMongoUtilities()
                              .getConnection(identity, cluster_name)
                              [_READINESS_DB][_READINESS_COLLECTION])
                collection.replace_one(
                    {"_id": _READINESS_ID},
                    {"_id": _READINESS_ID, "cluster_name": cluster_name,
                     "written_by": identity, "attempt": writes,
                     "written_at": datetime.now(timezone.utc)},
                    upsert=True)
                log.info("%s took a write after %.0fs, %d ping(s), %d write "
                         "attempt(s)", cluster_name, time.monotonic() - began,
                         pings, writes)
                return
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {str(exc)[:160]}"
            if due():
                log.info("still trying to write to %s after %.0fs, %d "
                         "attempt(s); last refusal %s", cluster_name,
                         time.monotonic() - began, writes, last)
            time.sleep(_RETRY_SECONDS)

    _raise_not_usable(cluster_name, pings, writes, last)


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume the pipeline cluster.")
    parser.add_argument("--cluster",
                        default=os.environ.get("PIPELINE_CLUSTER",
                                               "ChatHealthyDataPipelines"))
    parser.add_argument("--identity", default="pipelineEditor",
                        help="the identity the ping connects as")
    parser.add_argument("--interval", type=int, default=60,
                        help="seconds between attempts (default 60)")
    args = parser.parse_args()

    project = os.environ.get("ATLAS_PROJECT_ID", "").strip()
    public = os.environ.get("ATLAS_PIPELINE_PUBLIC_KEY", "").strip()
    private = os.environ.get("ATLAS_PIPELINE_PRIVATE_KEY", "").strip()
    if not (project and public and private):
        _raise_no_atlas_key()

    cluster = AtlasCluster(project, args.cluster, public, private)
    log.info("resuming %s; asking every %ds until it serves",
             args.cluster, args.interval)

    attempt = 0
    last_reason = None
    asked = False
    while True:
        attempt += 1
        try:
            state, paused = cluster.state()
        except Exception as exc:  # noqa: BLE001
            log.warning("attempt %d: could not read %s (%s: %s)",
                        attempt, args.cluster, type(exc).__name__, str(exc)[:120])
            time.sleep(args.interval)
            continue

        # Un-paused is where the Atlas API stops being consulted. From here
        # the cluster is asked directly: first a ping, then a write, each
        # polled every second until it answers or ten minutes are spent.
        if not paused:
            log.info("%s is un-paused after %d attempt(s); Atlas says %s. "
                     "Pinging every %ds.",
                     args.cluster, attempt, state, _PING_SECONDS)
            _wait_until_usable(args.cluster, args.identity)
            return 0

        reason = cluster.resume()
        if not reason:
            if not asked:
                log.info("attempt %d: resume accepted for %s", attempt, args.cluster)
                asked = True
            time.sleep(args.interval)
            continue

        if reason != last_reason:
            log.info("attempt %d: refused -- %s", attempt, reason)
            last_reason = reason
        elif attempt % 10 == 0:
            log.info("attempt %d: still refused -- %s", attempt, reason)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
