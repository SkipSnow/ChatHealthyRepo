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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import urllib.request

from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_log = ChatHealthyLoggingService()
STATUS_PORT = 6969

_IMDS_URL = ("http://169.254.169.254/metadata/instance/network/interface"
             "?api-version=2021-02-01")


def _controller_addresses() -> dict:
    """The addresses this box is actually at, read from Azure IMDS: the private
    IP bound on the NIC and the public IP the VNET node maps to it. This is how
    the log proves the controller is at the expected address rather than guessing
    it. Returns {} if IMDS is unreachable (e.g. off-Azure), never raising."""
    req = urllib.request.Request(_IMDS_URL, headers={"Metadata": "true"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    ipv4 = (data[0].get("ipv4") if data else {}) or {}
    addrs = (ipv4.get("ipAddress") or [{}])[0]
    return {"private": addrs.get("privateIpAddress", ""),
            "public": addrs.get("publicIpAddress", "")}

_DONE = ("completed", "succeeded", "done")
_FAILED = ("failed", "error")
_ACTIVE = ("claimed", "running", "in_progress")

# A worker is "live" if its last heartbeat is within this window; past it, a
# work_item that is still claimed/running is a worker that stopped reporting.
_HEARTBEAT_FRESH_SECONDS = 180
# No status read may take longer than this; past it the API answers
# status_read_timeout rather than hang (the bug that killed the last run).
_STATUS_READ_TIMEOUT_SECONDS = 8
# Run-record statuses that mean "admitted, not yet at fan-out".
_PRE_RUNNING = ("pending", "admitted", "provisioning",
                "pending_vm_provision", "starting")


def _is_fresh(hb_at, now: datetime.datetime) -> bool:
    """True if a heartbeat timestamp is within the freshness window."""
    if not hb_at:
        return False
    try:
        if isinstance(hb_at, str):
            hb_at = datetime.datetime.fromisoformat(hb_at.replace("Z", ""))
        return (now - hb_at).total_seconds() <= _HEARTBEAT_FRESH_SECONDS
    except Exception:  # noqa: BLE001
        return False


def _run_state(run: dict, steps: list, totals: dict) -> tuple[str, str]:
    """Where the run actually is, named and explained. The controller's API
    process is answering, so every state below is 'the controller is up, and
    here is why there is (or is not yet) worker status'. Returns
    (run_state, run_state_detail). Each branch is one of the legitimate reasons
    enumerated in the status design; none is collapsed into another."""
    status = (run.get("status") or "").lower()
    queue = totals["queue_depth"]
    claimed = totals["claimed_items"]
    active = totals["active_items"]
    fresh = totals["fresh_heartbeats"]
    ever = totals["heartbeats_ever"]
    current = totals.get("current_step") or "(none)"

    if status in _FAILED or status in ("aborted", "cancelled") \
            or run.get("fatal_exception") or any(s["failed"] for s in steps):
        return "fatal_error", ("the run failed or a step failed; see fatal_exception "
                               "and the per-step failed counts")
    if status in _DONE or (queue > 0 and all(
            s["state"] == "complete" for s in steps if s["total"])):
        return "complete", "every step is complete; the run finished"
    if queue == 0:
        if status in _PRE_RUNNING:
            return "admitted", ("the run is admitted and the controller is coming up "
                                "(waking the pipeline cluster); no work is queued yet")
        return "preparing", (f"serial pre-fan-out work is running (current step: "
                             f"{current}); these steps create no per-partition "
                             f"workers, so no worker status is expected yet")
    if claimed == 0 and ever == 0:
        return "workers_provisioning", (
            f"{queue} work item(s) are queued at {current} but none is claimed and "
            f"no heartbeat has arrived: the worker box is provisioning / cloud-init "
            f"is still starting worker_host")
    if claimed > 0 and fresh == 0 and ever == 0:
        return "claimed_no_heartbeat", (
            f"{claimed} item(s) are claimed at {current} but the first heartbeat "
            f"(120s cadence) has not landed yet")
    if active == 0 and fresh == 0:
        return "stalled", (
            f"{queue} item(s) at {current} are not progressing and no heartbeat is "
            f"fresh within {_HEARTBEAT_FRESH_SECONDS}s: the worker stopped reporting "
            f"or the worker box failed to come up")
    return "working", (f"{active} worker(s) active at {current}; "
                       f"{fresh} fresh heartbeat(s)")


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


_DB_LOCK = threading.Lock()
_DB = None


def _status_db():
    """The always-on front-end pipelineAdmin handle, opened once and reused.
    Opening a fresh mTLS connection -- a Key Vault cert fetch -- on every status
    call was slow enough to make the API look hung; it is cached here and
    reused. A failed open is not cached, so the next call retries."""
    global _DB
    if _DB is not None:
        return _DB
    with _DB_LOCK:
        if _DB is None:
            _DB = ChatHealthyMongoUtilities().getConnection(
                "pipelineEditor", "ChatHealthyFrontEnd")["pipelineAdmin"]
    return _DB


def build_status(step_names: list[str], run_id: str) -> dict | None:
    """The status document for one run: the run header, a top-level run_state
    (no_workers_yet | working | fatal_error | complete) and, for every step the
    pipeline's persistent configuration declares, the per-worker (per-partition)
    state and the percent complete. Returns None when no such run exists -- the
    caller renders that as an allocation-pending 200, never a 404."""
    db = _status_db()
    run = db["pipeline.runs"].find_one({"run_id": run_id})
    if not run:
        return None
    by_step: dict[str, list] = {}
    for item in db["pipeline.work_items"].find({"run_id": run_id}):
        by_step.setdefault(item.get("step"), []).append(item)
    names = list(step_names)
    for extra in sorted(set(by_step) - set(names) - {None}):
        names.append(extra)
    now = datetime.datetime.utcnow()
    queue_depth = active_items = claimed_items = fresh_heartbeats = heartbeats_ever = 0
    current_step = None
    steps = []
    for name in names:
        rows = by_step.get(name) or []
        total = len(rows)
        queue_depth += total
        done = sum(1 for r in rows if (r.get("status") or "") in _DONE)
        failed = sum(1 for r in rows if (r.get("status") or "") in _FAILED)
        active = sum(1 for r in rows if (r.get("status") or "") in _ACTIVE)
        active_items += active
        claimed_items += sum(1 for r in rows if (r.get("status") or "") in _ACTIVE)
        workers = []
        for r in rows:
            hb = r.get("heartbeat") or {}
            hb_at = r.get("heartbeat_at") or hb.get("at")
            if hb_at:
                heartbeats_ever += 1
            if _is_fresh(hb_at, now):
                fresh_heartbeats += 1
            workers.append({
                "partition": r.get("partition"),
                "status": r.get("status") or "unknown",
                "percent": 100 if (r.get("status") or "") in _DONE else 0,
                # Rich per-worker progress (LLD I.2.3.4), surfaced when present.
                "task": hb.get("task"),
                "scope": hb.get("scope"),
                "pass": hb.get("pass"),
                "records_done": hb.get("records_done"),
                "worker_state": hb.get("state"),
                "heartbeat_at": str(hb_at) if hb_at else None,
            })
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
        if current_step is None and state != "complete":
            current_step = name
        steps.append({"step": name, "state": state, "done": done,
                      "failed": failed, "total": total,
                      "percent": (100 * done // total) if total else 0,
                      "workers": workers})

    totals = {"queue_depth": queue_depth, "active_items": active_items,
              "claimed_items": claimed_items, "fresh_heartbeats": fresh_heartbeats,
              "heartbeats_ever": heartbeats_ever, "current_step": current_step}
    run_state, run_state_detail = _run_state(run, steps, totals)
    return {
        "run_id": run_id,
        "pipeline_name": run.get("pipeline_name"),
        "status": run.get("status"),
        # The run-level answer, inferred from the work_items the controller
        # wrote and the heartbeats workers left.
        "run_state": run_state,
        "run_state_detail": run_state_detail,
        # The controller's OWN account of itself -- it knows what it has done,
        # so this is authoritative, not inferred. worker_phase is set ONLY from
        # a probe of the box (fan_out_running means the pool PIDs were seen
        # there); box up is not process up.
        "controller_phase": run.get("controller_phase"),
        "controller_phase_detail": run.get("controller_phase_detail"),
        "controller_phase_at": str(run.get("controller_phase_at")) if run.get("controller_phase_at") else None,
        "worker_phase": run.get("worker_phase"),
        "worker_pool": run.get("worker_pool"),
        "worker_phase_detail": run.get("worker_phase_detail"),
        "worker_probe_at": str(run.get("worker_probe_at")) if run.get("worker_probe_at") else None,
        "states": run.get("states") or run.get("state_scope") or [],
        "data_version": run.get("data_version"),
        "started_at": str(run.get("started_at")),
        "ended_at": str(run.get("ended_at")) if run.get("ended_at") else None,
        "vm_name": run.get("vm_name"),
        "controller_heartbeat_at": str(run.get("controller_heartbeat_at")) if run.get("controller_heartbeat_at") else None,
        "queue_depth": queue_depth,
        "active_workers": active_items,
        "fresh_heartbeats": fresh_heartbeats,
        "served_by": "controller",
        "as_of": now.isoformat(timespec="seconds") + "Z",
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


def _status_with_timeout(step_names: list[str], run_id: str) -> tuple:
    """Run build_status under a hard wall-clock bound so the HTTP call can never
    hang on a slow Mongo read -- the failure that killed the last run. Returns
    (doc_or_None, error_or_None, timed_out)."""
    result: dict = {}

    def _work():
        try:
            result["doc"] = build_status(step_names, run_id)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(_STATUS_READ_TIMEOUT_SECONDS)
    if t.is_alive():
        return None, None, True
    return result.get("doc"), result.get("error"), False


def _make_handler(step_names: list[str]):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            started = time.monotonic()
            cn = _client_cn(self.connection)
            run_id = _run_id_from_path(self.path)
            code, state = 200, "unknown"
            try:
                # Auth and a missing argument are client-contract errors, not
                # run-state questions; everything else answers 200 with a state.
                if cn != _AUTHORIZED_CLIENT_CN:
                    code, state = 403, "forbidden"
                    self._json(403, {"error": f"client {cn or '(none)'} is not "
                                     f"authorized; only {_AUTHORIZED_CLIENT_CN}"})
                    return
                if not run_id:
                    code, state = 400, "bad_request"
                    self._json(400, {"error": "run_id is required"})
                    return
                doc, error, timed_out = _status_with_timeout(step_names, run_id)
                if timed_out:
                    state = "status_read_timeout"
                    _log.LogPipeline("ERROR", "run_status_server: status read exceeded "
                                     "%ds run_id=%s; answering status_read_timeout",
                                     _STATUS_READ_TIMEOUT_SECONDS, run_id)
                    self._json(200, {"run_id": run_id, "run_state": state,
                                     "served_by": "controller",
                                     "run_state_detail": f"the status read did not return "
                                     f"within {_STATUS_READ_TIMEOUT_SECONDS}s; the "
                                     f"controller is up, the store read is slow"})
                    return
                if error is not None:
                    state = "status_unavailable"
                    _log.LogPipeline("ERROR", "run_status_server: status read failed "
                                     "run_id=%s err=%s", run_id,
                                     f"{type(error).__name__}: {str(error)[:200]}")
                    self._json(200, {"run_id": run_id, "run_state": state,
                                     "served_by": "controller",
                                     "run_state_detail": f"{type(error).__name__}: "
                                     f"{str(error)[:200]}"})
                    return
                if doc is None:
                    state = "no_run_record"
                    self._json(200, {"run_id": run_id, "run_state": state,
                                     "served_by": "controller",
                                     "run_state_detail": "the controller is up but no "
                                     "pipeline.runs record exists for this run_id -- not "
                                     "yet admitted, already reaped, or wrong id"})
                    return
                state = doc.get("run_state", "unknown")
                self._json(200, doc)
            finally:
                dur_ms = int((time.monotonic() - started) * 1000)
                _log.LogPipeline("INFO", "run_status_server: GET run_id=%s cn=%s -> %d "
                                 "state=%s %dms", run_id or "(none)", cn or "(none)",
                                 code, state, dur_ms)

        def _json(self, code: int, body: dict) -> None:
            data = json.dumps(body, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:  # our own structured log replaces this
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
    # The address this controller came up on, from IMDS -- logged before the
    # bind so the run record shows exactly where the box is, not where we hope
    # it is. A client tapping the status API must reach this public IP.
    try:
        addrs = _controller_addresses()
        _log.LogPipeline(
            "INFO",
            "run_status_server: controller came up at private=%s public=%s; "
            "binding status listener on 0.0.0.0:%d",
            addrs.get("private") or "(none)", addrs.get("public") or "(none)",
            STATUS_PORT)
    except Exception as exc:  # noqa: BLE001
        _log.LogPipeline("WARNING",
                         "run_status_server: could not read controller address "
                         "from IMDS before bind: %s", str(exc)[:200])
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    ctx.load_verify_locations(cafile=ca)
    ctx.verify_mode = ssl.CERT_REQUIRED

    class _StatusServer(ThreadingHTTPServer):
        # A controller restarting on the same box must not fail to bind on a
        # lingering socket, and request threads must never block shutdown.
        allow_reuse_address = True
        daemon_threads = True

    httpd = _StatusServer(("0.0.0.0", STATUS_PORT), _make_handler(list(step_names)))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    def _serve_forever() -> None:
        # If the serve loop ever dies the API goes dark silently; say so loudly
        # so a lost status surface is visible, not a mystery hang to the client.
        try:
            httpd.serve_forever()
        except Exception as exc:  # noqa: BLE001
            _log.LogPipeline("ERROR", "run_status_server: serve loop exited: %s",
                             str(exc)[:300])

    thread = threading.Thread(target=_serve_forever, daemon=True,
                              name="run-status-server")
    thread.start()
    try:
        addrs = _controller_addresses()
        reachable = addrs.get("public") or addrs.get("private") or "(unknown)"
        _log.LogPipeline(
            "INFO",
            "run_status_server: mTLS listener UP, serving at %s:%d (%d steps); "
            "only client CN=%s is authorized",
            reachable, STATUS_PORT, len(step_names), _AUTHORIZED_CLIENT_CN)
    except Exception:  # noqa: BLE001
        _log.LogPipeline("INFO", "run_status_server: mTLS listener up on :%d (%d steps)",
                         STATUS_PORT, len(step_names))
    return httpd
