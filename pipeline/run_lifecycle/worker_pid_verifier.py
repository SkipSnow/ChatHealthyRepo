# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""worker_pid_verifier.py -- the Controller's zero-trust check that the Worker
box is actually running the worker program.

Provisioning a Worker VM is not proof that workers are running. The Controller
VERIFIES by looking at the box. It cannot get a handle to the worker's
container: the controller is in its own container on the Controller VM, and
worker_host runs in a different container on a different VM -- two boundaries,
no direct handle. The controller reaches the worker VM through the one channel
that crosses both, the Azure control plane: an ARM runCommand that executes on
the worker VM's HOST as root (the controller holds the pipelineController ARM
token that created the box; its container has no az CLI and no SSH key, and
needs neither for this).

Box up is not process up. The container launching is not the fan-out taking
place: worker_host spawns a bounded pool of pipeline_worker.py processes, one
per claimed partition, and THAT pool -- not the container, not worker_host's
launch -- is the proof that workers are running and the fan-out happened. So
the probe counts `pipeline_worker.py` PIDs INSIDE the worker container. The
handle to that container is reached through the control plane: runCommand runs
on the host, and the host's own docker daemon answers
`docker exec <cid> pgrep -fc pipeline_worker.py`. The count is the fan-out width
on the box: 0 means no fan-out (no worker process, whatever the box is doing),
N means N workers are running.

A run is not reported `workers_running` until this returns a positive pool
count. Best-effort and self-bounded: a slow or failed probe never blocks the
run; it returns a negative verdict with the reason, and the caller tries again.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pipeline.run_lifecycle.worker_vm_provisioning import (
    _ARM, _VM_API, _controller_arm_token, _req, _short)

_log = ChatHealthyLoggingService()

# The program whose PIDs prove the fan-out took place. pipeline_worker.py is
# the pool process worker_host spawns one-per-claimed-partition; its count IS
# the fan-out width on the box. worker_host.py is reported too (the resident
# host that spawned the pool), but the pool count is the gate.
_POOL_PROGRAM = "pipeline_worker.py"
_HOST_PROGRAM = "worker_host.py"

# The probe, run as root on the Worker VM's host by RunShellScript. It finds the
# one worker container and, through the host's own docker daemon (the handle to
# the container the controller cannot reach directly), counts the pool processes
# INSIDE it, then prints one machine-readable line this module parses -- no
# scraping of free-form ps output. Every branch prints the line so the verdict
# is never silence.
_PROBE_SCRIPT = (
    'cid=$(docker ps -q 2>/dev/null | head -n1); '
    'if [ -z "$cid" ]; then '
    'echo "CHPROBE container=0 pool=0 host=0"; exit 0; fi; '
    'pool=$(docker exec "$cid" pgrep -fc pipeline_worker.py 2>/dev/null || echo 0); '
    'host=$(docker exec "$cid" pgrep -fc worker_host.py 2>/dev/null || echo 0); '
    'echo "CHPROBE container=1 pool=$pool host=$host"'
)

_POLL_SLEEP_S = 3


def _run_command(vm_name: str, script: str, tok: str, timeout_s: int) -> str:
    """POST a RunShellScript to the Worker box and return its stdout.

    runCommand is a long-running ARM operation: the POST returns 202 with a
    Location (the operation result) and an Azure-AsyncOperation (the status).
    We poll the Location until it returns 200 with the runCommand result, whose
    value[] carries the StdOut component. Returns "" on any failure -- the
    caller renders that as an unverified verdict, never a crash."""
    sub = _req("AZURE_SUBSCRIPTION_ID")
    rg = _req("AZURE_RESOURCE_GROUP")
    url = (f"{_ARM}/subscriptions/{sub}/resourceGroups/{rg}/providers/"
           f"Microsoft.Compute/virtualMachines/{vm_name}/runCommand"
           f"?api-version={_VM_API}")
    body = json.dumps({"commandId": "RunShellScript",
                       "script": [script]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json"})
    deadline = time.monotonic() + timeout_s
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status == 200:
            return _stdout_of(json.loads(resp.read().decode("utf-8") or "{}"))
        location = resp.headers.get("Location") or resp.headers.get("Azure-AsyncOperation")
    if not location:
        return ""
    while time.monotonic() < deadline:
        time.sleep(_POLL_SLEEP_S)
        poll = urllib.request.Request(
            location, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(poll, timeout=30) as g:
            if g.status == 200:
                doc = json.loads(g.read().decode("utf-8") or "{}")
                if "value" in doc:
                    return _stdout_of(doc)
                # Azure-AsyncOperation gives only {status}; a Succeeded there
                # with no value means we polled the status URL, not the result.
                if (doc.get("status") or "").lower() in ("failed", "canceled"):
                    return ""
    return ""


def _stdout_of(result: dict) -> str:
    """The StdOut component message from a runCommand result document."""
    for item in (result or {}).get("value", []):
        if "StdOut" in (item.get("code") or ""):
            return item.get("message") or ""
    return ""


def _parse_probe(stdout: str) -> dict:
    """Parse the single CHPROBE line into its counts. Absent line -> all zero."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("CHPROBE "):
            fields = {}
            for tok in line[len("CHPROBE "):].split():
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    fields[k] = int(v) if v.isdigit() else 0
            return fields
    return {}


def probe_worker_box(run_id: str, *, timeout_s: int = 120) -> dict:
    """Look at the Worker box and report whether the fan-out took place.

    Returns {verified, pool, host, container, vm_name, detail}. verified is True
    only when pipeline_worker.py pool PIDs are present inside the worker
    container (pool >= 1) -- the fan-out, the authoritative 'workers_running'
    signal. A container that is up with no pool is NOT verified: box up is not
    process up. Never raises: an ARM failure returns verified=False with the
    reason in detail, so the caller re-probes rather than the run dying."""
    vm_name = f"vm-chworker-{_short(run_id)}"[:63]
    try:
        tok = _controller_arm_token()
        stdout = _run_command(vm_name, _PROBE_SCRIPT, tok, timeout_s)
    except urllib.error.HTTPError as exc:
        detail = f"ARM runCommand HTTP {exc.code} probing {vm_name}"
        _log.LogPipeline("WARNING", "worker_pid_verifier: %s run_id=%s", detail, run_id)
        return {"verified": False, "pool": 0, "host": 0,
                "container": False, "vm_name": vm_name, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {str(exc)[:200]}"
        _log.LogPipeline("WARNING", "worker_pid_verifier: probe failed run_id=%s %s",
                         run_id, detail)
        return {"verified": False, "pool": 0, "host": 0,
                "container": False, "vm_name": vm_name, "detail": detail}

    fields = _parse_probe(stdout)
    if not fields:
        detail = "no CHPROBE line in runCommand output (box reachable, probe silent)"
        _log.LogPipeline("WARNING", "worker_pid_verifier: %s run_id=%s vm=%s",
                         detail, run_id, vm_name)
        return {"verified": False, "pool": 0, "host": 0,
                "container": False, "vm_name": vm_name, "detail": detail}

    container = bool(fields.get("container"))
    pool = fields.get("pool", 0)
    host = fields.get("host", 0)
    verified = pool >= 1
    if verified:
        detail = (f"fan-out took place: {pool} {_POOL_PROGRAM} process(es) "
                  f"(+{host} {_HOST_PROGRAM}) inside the worker container on {vm_name}")
    elif not container:
        detail = f"no docker container up on {vm_name} yet (box still provisioning)"
    elif host >= 1:
        detail = (f"worker container up and {_HOST_PROGRAM} running on {vm_name}, but "
                  f"0 {_POOL_PROGRAM} -- host is up, fan-out has NOT happened yet")
    else:
        detail = (f"worker container up on {vm_name} but neither {_HOST_PROGRAM} nor "
                  f"{_POOL_PROGRAM} is running -- box up, process not up")
    _log.LogPipeline("INFO", "worker_pid_verifier: run_id=%s vm=%s verified=%s (%s)",
                     run_id, vm_name, verified, detail)
    return {"verified": verified, "pool": pool, "host": host,
            "container": container, "vm_name": vm_name, "detail": detail}
