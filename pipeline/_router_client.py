# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Shared HTTP client for triggering and polling Azure Functions pipeline
orchestrators via the Router endpoint.

Used by every `pipeline/run_*.py` script. NOT shipped to Azure (lives
in `pipeline/`, not `pipeline/Code/`).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Dev Azure Function App Router URL (qa/prod hosts get added when those
# Function Apps are provisioned).
ROUTER_URL = "https://devpipelinemanagmentservice-hqa9f5b0b7b4hqgg.eastus2-01.azurewebsites.net/api/Router"

# Force UTF-8 stdout so step notices with em-dash/arrow/ellipsis don't
# UnicodeEncodeError on Windows cp1252 consoles.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _bearer_token() -> str:
    """Read bearer token from `<repo_root>/pipeline.http` (single line)."""
    repo_root = Path(__file__).resolve().parent.parent
    p = repo_root / "pipeline.http"
    if not p.is_file():
        sys.exit(
            f"ERROR: {p} not found.\n"
            f"Create it with a single line: Bearer <your-token>"
        )
    raw = p.read_text(encoding="utf-8").strip()
    if raw.startswith("Bearer "):
        raw = raw[7:].strip()
    if not raw:
        sys.exit(f"ERROR: {p} is empty.")
    return raw


def start_orchestrator(task: str, payload: dict) -> dict:
    """POST to the Router. Returns the 202 response body (instance id +
    statusQueryGetUri + raiseEventPostUri + terminatePostUri + ...).
    """
    token = _bearer_token()
    body = json.dumps({"ChatHealthyTask": task, "payload": payload}).encode("utf-8")
    req = urllib.request.Request(
        ROUTER_URL, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(
            f"ERROR: Router returned HTTP {e.code} for task {task!r}\n"
            f"  body: {e.read().decode('utf-8', errors='replace')[:1000]}"
        )


def poll_until_terminal(status_url: str, poll_interval_s: int = 20) -> dict:
    """Poll the orchestrator's status URL until runtimeStatus is one of
    Completed / Failed / Terminated. Prints each tick. Returns the final
    status document.
    """
    while True:
        try:
            with urllib.request.urlopen(status_url, timeout=20) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            print(f"  poll error ({type(e).__name__}: {e}) — retry in {poll_interval_s}s")
            time.sleep(poll_interval_s)
            continue
        status = doc.get("runtimeStatus", "?")
        custom = doc.get("customStatus") or "(no custom status)"
        print(f"  [{status}] {custom}", flush=True)
        if status in ("Completed", "Failed", "Terminated"):
            return doc
        time.sleep(poll_interval_s)


def status_url_for_instance(instance_id: str) -> str:
    """Build the durabletask status URL for an existing instance. The
    master key from `pipeline.http` is NOT the durabletask code; this
    is unauthenticated from outside Azure so we use the function
    master key plumbed in via env."""
    import os
    code = os.environ.get("AZURE_DURABLE_TASK_CODE")
    if not code:
        sys.exit(
            "ERROR: AZURE_DURABLE_TASK_CODE not set. Capture it from the "
            "statusQueryGetUri the Router returns on the first start, or "
            "fetch via `az functionapp keys list`."
        )
    return (
        f"https://devpipelinemanagmentservice-hqa9f5b0b7b4hqgg.eastus2-01.azurewebsites.net"
        f"/runtime/webhooks/durabletask/instances/{instance_id}"
        f"?taskHub=DevPipelineManagmentService&connection=Storage&code={code}"
    )


def run_pipeline(task: str, payload: dict) -> int:
    """High-level wrapper: start the orchestrator, poll until terminal,
    return 0 on Completed else 1. Prints output on terminal."""
    print(f"[{task}] payload: {json.dumps(payload)}", flush=True)
    started = start_orchestrator(task, payload)
    instance_id = started.get("id")
    status_url = started["statusQueryGetUri"]
    print(f"[{task}] instance: {instance_id}", flush=True)
    final = poll_until_terminal(status_url)
    rs = final.get("runtimeStatus", "?")
    print(f"[{task}] terminal: {rs}", flush=True)
    output = final.get("output")
    if output is not None:
        # Output is often a JSON-encoded string; pretty-print best-effort.
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except Exception:
                pass
        print(f"[{task}] output:\n{json.dumps(output, indent=2, default=str)[:4000]}", flush=True)
    return 0 if rs == "Completed" else 1
