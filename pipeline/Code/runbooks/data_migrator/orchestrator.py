"""orchestrator.py — ChatHealthyDataMigrator orchestrator runbook.

Runs on the standard Azure sandbox of ChatHealthyJobManager. The gateway
function app posts a webhook to this runbook to start a migration job.
The orchestrator:

  1. Receives the migration payload from the gateway (job_id + every
     migration arg).
  2. Starts the provisioner runbook on the standard Azure sandbox and
     polls until it reaches a terminal state. Provisioner success means
     the Hybrid Worker VM is created and registered with the
     ChatHealthyDataMigratorWorkGroup.
  3. Fires the migrator runbook on the new Hybrid Worker (fire and
     forget — the migration is long-running and the standard sandbox
     has a 3-hour wall-clock cap, so the orchestrator must not wait).
  4. Exits 0.

Failure modes propagate as runbook exit 1. The gateway returns 202 +
job_id the moment the webhook accepts the call, so a failure here is
visible to the operator via the Automation Account job state but the
caller sees only an accepted-but-failed job.

Input parameters (webhook JSON body or job parameters): the full
migration arg set the gateway forwarded — job_id, source/destination
cluster info, filter, thread_criteria, preserve_indices,
reservation_duration_minutes, env_prefix, build_id, request_guid.

Environment (Automation Variables):
    AZ_SUBSCRIPTION_ID    — hosts ChatHealthyJobManager.
    AZ_RESOURCE_GROUP     — ChatHhealthyResorceManager (Automation Account RG).
    AZ_AUTOMATION_ACCOUNT — ChatHealthyJobManager.
"""
import json
import logging
import os
import sys
import time
import traceback

import requests

try:
    import automationassets
    for k in ("AZ_SUBSCRIPTION_ID", "AZ_RESOURCE_GROUP", "AZ_AUTOMATION_ACCOUNT"):
        try:
            os.environ[k] = str(automationassets.get_automation_variable(k))
        except Exception:
            pass
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("orchestrator")

_AUTOMATION_API = "2023-11-01"
_PROVISIONER_RUNBOOK = "ChatHealthyDataMigratorProvisioner"
_MIGRATOR_RUNBOOK = "ChatHealthyDataMigrator"
_HYBRID_WORKER_GROUP = "ChatHealthyDataMigratorWorkGroup"

# Polling cadence for provisioner job. Provisioning a VM + installing the
# Hybrid Worker extension is in the 5-10 min range; poll every 15s gives
# the orchestrator ~720 polls before the standard-sandbox 3-hour cap, well
# above what provisioning needs.
_PROVISIONER_POLL_SEC = 15
_PROVISIONER_TIMEOUT_SEC = 30 * 60


def _mi_token() -> str:
    endpoint = os.environ["IDENTITY_ENDPOINT"]
    header = os.environ["IDENTITY_HEADER"]
    r = requests.get(
        endpoint,
        params={"resource": "https://management.azure.com/", "api-version": "2019-08-01"},
        headers={"X-IDENTITY-HEADER": header},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _aa_base(sub: str, rg: str, aa: str) -> str:
    return (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/resourceGroups/{rg}/providers/Microsoft.Automation"
        f"/automationAccounts/{aa}"
    )


def _start_runbook(sub: str, rg: str, aa: str, runbook: str,
                   parameters: dict, run_on: str | None = None) -> str:
    """Start a runbook job. `parameters` becomes the runbook's input parameters
    (all values stringified — Automation parameter values are strings on the
    wire). `run_on` specifies a Hybrid Worker group; None means the standard
    Azure sandbox. Returns the Automation job_id."""
    import uuid as _uuid
    job_id = str(_uuid.uuid4())
    url = f"{_aa_base(sub, rg, aa)}/jobs/{job_id}?api-version={_AUTOMATION_API}"
    body = {
        "properties": {
            "runbook": {"name": runbook},
            "parameters": {k: str(v) for k, v in parameters.items()},
        }
    }
    if run_on:
        body["properties"]["runOn"] = run_on
    token = _mi_token()
    r = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body, timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"Start runbook {runbook!r} failed: HTTP {r.status_code} {r.text[:500]}"
        )
    log.info("Started runbook %s as Automation job %s (run_on=%s)",
             runbook, job_id, run_on or "AzureSandbox")
    return job_id


def _get_job_status(sub: str, rg: str, aa: str, automation_job_id: str) -> str:
    url = (
        f"{_aa_base(sub, rg, aa)}/jobs/{automation_job_id}"
        f"?api-version={_AUTOMATION_API}"
    )
    token = _mi_token()
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("properties", {}).get("status", "Unknown")


def _wait_for_terminal(sub: str, rg: str, aa: str, automation_job_id: str) -> str:
    deadline = time.time() + _PROVISIONER_TIMEOUT_SEC
    terminal = {"Completed", "Failed", "Stopped", "Suspended"}
    while time.time() < deadline:
        status = _get_job_status(sub, rg, aa, automation_job_id)
        if status in terminal:
            log.info("Provisioner job %s reached terminal state: %s",
                     automation_job_id, status)
            return status
        time.sleep(_PROVISIONER_POLL_SEC)
    raise RuntimeError(
        f"Provisioner job {automation_job_id} did not reach terminal state "
        f"within {_PROVISIONER_TIMEOUT_SEC}s"
    )


def _extract_payload(webhook_data, job_params) -> dict:
    if webhook_data:
        try:
            return json.loads(webhook_data.get("RequestBody", "{}"))
        except Exception:
            pass
    return dict(job_params or {})


def _vm_name_for_job(job_id: str) -> str:
    # Deterministic short VM name from the gateway-minted job_id so every
    # downstream runbook (provisioner, migrator, deprovisioner) computes the
    # same name without separately passing it around.
    return f"chdm-{job_id[:8]}"


def _main(webhook_data, job_params):
    payload = _extract_payload(webhook_data, job_params)
    job_id = payload.get("job_id")
    if not job_id:
        raise RuntimeError("orchestrator: job_id missing from payload")

    sub = os.environ["AZ_SUBSCRIPTION_ID"]
    rg = os.environ["AZ_RESOURCE_GROUP"]
    aa = os.environ["AZ_AUTOMATION_ACCOUNT"]
    vm_name = _vm_name_for_job(job_id)

    log.info("Orchestrator begin: job_id=%s vm_name=%s", job_id, vm_name)

    prov_job_id = _start_runbook(
        sub, rg, aa, _PROVISIONER_RUNBOOK,
        parameters={"job_id": job_id, "vm_name": vm_name},
        run_on=None,
    )
    status = _wait_for_terminal(sub, rg, aa, prov_job_id)
    if status != "Completed":
        raise RuntimeError(
            f"Provisioner job {prov_job_id} ended {status} (expected Completed)"
        )

    migrator_payload = dict(payload)
    migrator_payload["vm_name"] = vm_name
    migrator_job_id = _start_runbook(
        sub, rg, aa, _MIGRATOR_RUNBOOK,
        parameters=migrator_payload,
        run_on=_HYBRID_WORKER_GROUP,
    )
    log.info("Orchestrator complete: handed off to migrator job %s on %s",
             migrator_job_id, _HYBRID_WORKER_GROUP)
    print(json.dumps({
        "orchestrator_status": "ok",
        "job_id": job_id,
        "vm_name": vm_name,
        "provisioner_automation_job_id": prov_job_id,
        "migrator_automation_job_id": migrator_job_id,
    }), flush=True)


try:
    _wd = None
    try:
        _wd = WebhookData  # noqa: F821 — provided by AA when invoked via webhook
    except NameError:
        pass
    _main(_wd, None)
    sys.exit(0)
except Exception:
    tb = traceback.format_exc()
    log.error("Orchestrator failed: %s", tb)
    print(json.dumps({"orchestrator_status": "error", "error": tb[-1500:]}), flush=True)
    sys.exit(1)
