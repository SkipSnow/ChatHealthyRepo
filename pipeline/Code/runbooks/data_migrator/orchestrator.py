"""orchestrator.py - ChatHealthyDataMigrator orchestrator runbook.

Runs on the standard Azure sandbox of ChatHealthyJobManager. The gateway
function app posts a webhook to this runbook to start a migration job.
The orchestrator fires the provisioner runbook (fire-and-forget) and
exits. No wait. The provisioner ends by firing the migrator on the
Hybrid Worker group; the migrator ends by firing the deprovisioner.
Chain-fire pattern.

Input payload: Azure Automation webhook-triggered Python runbooks receive
a WebhookData wrapper at sys.argv[1] (a JSON string with WebhookName,
RequestBody, RequestHeader). The gateway POSTs the migration payload as
the request body; we unwrap WebhookData.RequestBody and json.loads it.
Fields inside RequestBody:
    job_id           - gateway-minted external job id.
    request_guid     - gateway request guid.
    router_build_id  - gateway-read build id.
    Plus every migration arg the gateway forwarded:
      source_cluster, source_database, source_collection,
      destination_cluster, destination_database, destination_collection,
      filter, thread_criteria, preserve_indices,
      reservation_duration_minutes.

Environment (Automation Variables):
    AZ_SUBSCRIPTION_ID    - hosts ChatHealthyJobManager.
    AZ_RESOURCE_GROUP     - Automation Account resource group.
    AZ_AUTOMATION_ACCOUNT - ChatHealthyJobManager.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import traceback
import uuid

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

_request_guid = "?"


class _RGFilter(logging.Filter):
    def filter(self, record):
        record.request_guid = _request_guid
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [rg=%(request_guid)s] %(message)s",
)
log = logging.getLogger("orchestrator")
log.addFilter(_RGFilter())

_AUTOMATION_API = "2023-11-01"
_PROVISIONER_RUNBOOK = "ChatHealthyDataMigratorProvisioner"


def _read_payload() -> dict:
    """Orchestrator is webhook-only. Legacy Azure Automation's webhook
    delivery to a Python runbook mangles the WebhookData wrapper at
    sys.argv[1]: keys lose quotes, string values lose quotes, bools
    become lowercase - the wrapper is NOT valid JSON. But the
    WebhookData.RequestBody value (the original HTTP POST body the
    gateway sent) is embedded verbatim with its JSON quotes intact.

    Strategy: find the literal "RequestBody:" marker in sys.argv[1],
    then use json.JSONDecoder.raw_decode to consume exactly the embedded
    JSON object that follows. The decoder stops at the JSON's matching
    close brace, ignoring the rest of the mangled AA wrapper."""
    if len(sys.argv) < 2:
        raise RuntimeError("no payload: sys.argv[1] missing")
    s = sys.argv[1]
    marker = "RequestBody:"
    idx = s.find(marker)
    if idx == -1:
        raise RuntimeError(
            f"orchestrator: sys.argv[1] missing 'RequestBody:' marker; "
            f"first 500 chars={s[:500]!r}"
        )
    rest = s[idx + len(marker):].lstrip()
    try:
        body, _consumed = json.JSONDecoder().raw_decode(rest)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"orchestrator: RequestBody is not parseable JSON; first 500 "
            f"chars after marker={rest[:500]!r}; error={e}"
        )
    if not isinstance(body, dict):
        raise RuntimeError(
            f"orchestrator: RequestBody is not a dict; got {type(body).__name__}"
        )
    return body


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


def _vm_name_for_job(job_id: str) -> str:
    # Deterministic short VM name from the job_id so every downstream
    # runbook computes the same name without re-passing.
    return f"chdm-{job_id[:8]}"


def _start_runbook_fire_and_forget(sub: str, rg: str, aa: str, runbook: str,
                                   downstream_payload: dict,
                                   run_on: str | None = None) -> str:
    """Start a runbook job via REST API. Returns the AA job_id immediately.
    Does NOT poll for completion. `downstream_payload` is JSON-encoded into
    a single `payload` parameter - the receiving runbook reads it via
    json.loads(sys.argv[1])."""
    aa_job_id = str(uuid.uuid4())
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/jobs/{aa_job_id}?api-version={_AUTOMATION_API}"
    )
    # Base64-encode the payload JSON. Legacy Azure Automation Python
    # runbook invocation strips quotes from PUT-/jobs parameter values
    # (sys.argv[1] arrives as `{key: value}` with quotes mangled). Base64
    # uses only alphanumerics + `+/=`, none of which AA mangles. The
    # downstream runbook decodes via base64.b64decode + json.loads.
    encoded = base64.b64encode(json.dumps(downstream_payload).encode("utf-8")).decode("ascii")
    body = {
        "properties": {
            "runbook": {"name": runbook},
            "parameters": {"payload": encoded},
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
    return aa_job_id


def _main():
    global _request_guid
    payload = _read_payload()
    _request_guid = payload.get("request_guid", "?")
    if payload.get("health_check") is True:
        log.info("health_check fired on orchestrator runbook")
        return
    job_id = payload.get("job_id")
    if not job_id:
        raise RuntimeError("orchestrator: job_id missing from payload")
    vm_name = _vm_name_for_job(job_id)
    payload["vm_name"] = vm_name

    sub = os.environ["AZ_SUBSCRIPTION_ID"]
    rg = os.environ["AZ_RESOURCE_GROUP"]
    aa = os.environ["AZ_AUTOMATION_ACCOUNT"]

    log.info("Orchestrator begin: job_id=%s vm_name=%s", job_id, vm_name)

    provisioner_aa_job_id = _start_runbook_fire_and_forget(
        sub, rg, aa, _PROVISIONER_RUNBOOK, payload, run_on=None,
    )
    log.info("Orchestrator fired provisioner: job_id=%s provisioner_aa_job_id=%s",
             job_id, provisioner_aa_job_id)
    print(json.dumps({
        "orchestrator_status": "ok",
        "job_id": job_id,
        "vm_name": vm_name,
        "provisioner_aa_job_id": provisioner_aa_job_id,
    }), flush=True)


try:
    _main()
    sys.exit(0)
except Exception:
    tb = traceback.format_exc()
    log.error("Orchestrator failed: %s", tb)
    print(json.dumps({"orchestrator_status": "error", "error": tb[-1500:]}), flush=True)
    sys.exit(1)
