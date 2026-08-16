# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Resume the pipeline cluster, and keep asking until it serves.

The mirror of pipeline_pause_cluster.py. Atlas refuses a resume for reasons
that pass on their own -- a cluster still UPDATING from the pause, or resumed
so recently the request collides with itself -- so this does not give up
either. It asks every minute until the cluster reports paused=False and
stateName IDLE, which is the point at which a connection will actually be
served rather than time out on server selection.

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
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

log = ChatHealthyLoggingService()

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume the pipeline cluster.")
    parser.add_argument("--cluster",
                        default=os.environ.get("PIPELINE_CLUSTER",
                                               "ChatHealthyDataPipelines"))
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

        # Not paused and IDLE is the only pair that serves a connection. Not
        # paused while UPDATING is a cluster still coming up, and a client that
        # connects then fails server selection.
        if not paused and state == "IDLE":
            log.info("%s is RUNNING after %d attempt(s)", args.cluster, attempt)
            return 0

        if not paused:
            log.info("attempt %d: %s is %s; waiting", attempt, args.cluster, state)
            time.sleep(args.interval)
            continue

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
