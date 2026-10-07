"""run_status_server.py -- the run-status listener a controller stands up.

EPIC-010-F-001-S-015: the controller MUST answer the status call once it is
established (REQ-B-003). This is that listener: an mTLS HTTPS server bound on the
controller VM at port 6969 that, given one argument -- the run_id (REQ-B-002) --
returns the per-step, per-worker status of the run (REQ-B-004), read live from
the run's work_items on the always-on front-end cluster.

It serves over the controller's own certificate (placed by bootstrap at
CHATHEALTHY_CERT_PATH/KEY_PATH, CA chain at CHATHEALTHY_CA_CHAIN_PATH) and
requires a client certificate signed by the same CA -- mutual auth. The server
runs on a daemon thread so it lives exactly as long as the controller process.

Request:  GET /status?run_id=<id>   (or GET /runs/<id>/status)
Response: 200 application/json -- the status document; 400 no run_id; 404 no run.
"""
from __future__ import annotations

import datetime
import json
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_log = ChatHealthyLoggingService()
STATUS_PORT = 6969

_DONE = ("completed", "succeeded", "done")
_FAILED = ("failed", "error")
_ACTIVE = ("claimed", "running", "in_progress")


def _run_id_from_path(path: str) -> str | None:
    """The single argument: run_id. Accepts /status?run_id=X and
    /runs/<id>/status. No other argument is read (REQ-B-002)."""
    parsed = urlparse(path)
    qs = parse_qs(parsed.query)
    if qs.get("run_id"):
        return qs["run_id"][0]
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "runs":
        return parts[1]
    return None


def build_status(step_names: list[str], run_id: str) -> dict | None:
    """The status document for one run: the run header plus, for every step the
    pipeline's persistent configuration declares, the per-worker (per-partition)
    state and the percent complete. Returns None when no such run exists."""
    db = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyFrontEnd")["pipelineAdmin"]
    run = db["pipeline.runs"].find_one({"run_id": run_id})
    if not run:
        return None
    by_step: dict[str, list] = {}
    for item in db["pipeline.work_items"].find({"run_id": run_id}):
        by_step.setdefault(item.get("step"), []).append(item)
    names = list(step_names)
    for extra in sorted(set(by_step) - set(names) - {None}):
        names.append(extra)
    steps = []
    for name in names:
        rows = by_step.get(name) or []
        total = len(rows)
        done = sum(1 for r in rows if (r.get("status") or "") in _DONE)
        failed = sum(1 for r in rows if (r.get("status") or "") in _FAILED)
        active = sum(1 for r in rows if (r.get("status") or "") in _ACTIVE)
        workers = [{"partition": r.get("partition"),
                    "status": r.get("status") or "unknown",
                    "percent": 100 if (r.get("status") or "") in _DONE else 0}
                   for r in rows]
        if total == 0:
            state = "not_started"
        elif failed:
            state = "failed"
        elif done == total:
            state = "complete"
        elif active or done:
            state = "running"
        else:
            state = "queued"
        steps.append({"step": name, "state": state, "done": done,
                      "failed": failed, "total": total,
                      "percent": (100 * done // total) if total else 0,
                      "workers": workers})
    return {
        "run_id": run_id,
        "pipeline_name": run.get("pipeline_name"),
        "status": run.get("status"),
        "states": run.get("states") or run.get("state_scope") or [],
        "data_version": run.get("data_version"),
        "started_at": str(run.get("started_at")),
        "served_by": "controller",
        "as_of": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "steps": steps,
    }


_AUTHORIZED_CLIENT_CN = "claudeCodeAgent"


def _client_cn(conn) -> str:
    """The commonName of the presented client certificate, or '' if none."""
    cert = conn.getpeercert() if conn else None
    for rdn in (cert or {}).get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return ""


def _make_handler(step_names: list[str]):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            cn = _client_cn(self.connection)
            if cn != _AUTHORIZED_CLIENT_CN:
                self._json(403, {"error": f"client {cn or '(none)'} is not "
                                          f"authorized; only {_AUTHORIZED_CLIENT_CN}"})
                return
            run_id = _run_id_from_path(self.path)
            if not run_id:
                self._json(400, {"error": "run_id is required"})
                return
            try:
                doc = build_status(step_names, run_id)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})
                return
            if doc is None:
                self._json(404, {"error": f"no run {run_id}"})
                return
            self._json(200, doc)

        def _json(self, code: int, body: dict) -> None:
            data = json.dumps(body, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:  # silence stderr spam
            return

    return _Handler


def start_status_server(step_names: list[str]):
    """Stand up the mTLS status listener on a daemon thread. Fails loud if the
    controller certificate is not in the environment -- a controller that cannot
    answer its status call must say so, not run a plaintext or absent server."""
    cert = os.environ.get("CHATHEALTHY_CERT_PATH", "").strip()
    key = os.environ.get("CHATHEALTHY_KEY_PATH", "").strip()
    ca = os.environ.get("CHATHEALTHY_CA_CHAIN_PATH", "").strip()
    missing = [n for n, v in (("CHATHEALTHY_CERT_PATH", cert),
                              ("CHATHEALTHY_KEY_PATH", key),
                              ("CHATHEALTHY_CA_CHAIN_PATH", ca)) if not v]
    if missing:
        from chathealthy_lib.exceptions import ChatHealthyException
        raise ChatHealthyException(
            mode="config_error",
            message="run_status_server: cannot start the status listener; "
                    + ", ".join(missing) + " absent from the controller environment",
            component="run_status_server", missing=",".join(missing))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    ctx.load_verify_locations(cafile=ca)
    ctx.verify_mode = ssl.CERT_REQUIRED
    httpd = ThreadingHTTPServer(("0.0.0.0", STATUS_PORT), _make_handler(list(step_names)))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                              name="run-status-server")
    thread.start()
    _log.LogPipeline("INFO", "run_status_server: mTLS listener up on :%d (%d steps)",
                     STATUS_PORT, len(step_names))
    return httpd
