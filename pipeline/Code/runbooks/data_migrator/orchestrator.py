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
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

import base64
import json
import os
import sys
import traceback
import uuid

import requests
from requests.auth import HTTPDigestAuth

try:
    import automationassets
    for k in ("AZ_SUBSCRIPTION_ID", "AZ_RESOURCE_GROUP", "AZ_AUTOMATION_ACCOUNT",
              "ATLAS_PUBLIC_KEY", "ATLAS_PRIVATE_KEY", "ATLAS_PROJECT_ID",
              "KEY_VAULT_URI", "AUTOMATION_ENV_PREFIX"):
        try:
            os.environ[k] = str(automationassets.get_automation_variable(k))
        except Exception:
            pass
except ImportError:
    pass

# Wire Mongo logging BEFORE ChatHealthyLoggingService() below. See
# deprovisioner.py for the fallback-to-stderr contract.
os.environ.setdefault("CH_SPACE_NAME", "data-migrator-orchestrator")
os.environ.setdefault("CH_COMPONENT", "data-migrator-orchestrator")
os.environ.setdefault("ENV_PREFIX",
                      os.environ.get("AUTOMATION_ENV_PREFIX", "dev"))
try:
    from chathealthy_lib.pipeline_boot import bootstrap_aa_mongo_logging as _boot
    _boot(
        component_name="data-migrator-orchestrator",
        env_prefix=os.environ.get("AUTOMATION_ENV_PREFIX", "dev"),
    )
except Exception as _bootstrap_exc:  # noqa: BLE001
    sys.stderr.write(
        f"orchestrator: bootstrap_aa_mongo_logging failed "
        f"({type(_bootstrap_exc).__name__}: {_bootstrap_exc}); "
        "continuing with stderr-only logging.\n"
    )
    os.environ["CH_LOG_DESTINATION"] = "stderr"

log = ChatHealthyLoggingService()
_AUTOMATION_API = "2023-11-01"
_PROVISIONER_RUNBOOK = "ChatHealthyDataMigratorProvisioner"


def _read_payload() -> dict:
    """Orchestrator is webhook-only. Legacy Azure Automation's webhook
    delivery to a Python runbook does two things simultaneously:
      (1) Mangles the WebhookData wrapper into a non-JSON format with
          unquoted keys and string values.
      (2) Shell-splits that mangled wrapper on commas, distributing
          chunks across sys.argv[1], sys.argv[2], ...
    Empirically: a single 24-byte HTTP body {"health_check": true} comes
    back to the runbook split into 4+ argv entries because the mangled
    wrapper has commas between fields.

    Reconstruct: re-join sys.argv[1:] with ' ' (a single space - AA's
    shell encoding splits the mangled wrapper on whitespace, so the
    original spaces inside the embedded JSON body become argv separators),
    locate "RequestBody:", and use json.JSONDecoder.raw_decode to consume
    the embedded JSON object."""
    if len(sys.argv) < 2:
        raise ChatHealthyException(mode="runtime_error", message="no payload: sys.argv[1] missing")
    s = " ".join(sys.argv[1:])
    marker = "RequestBody:"
    idx = s.find(marker)
    if idx == -1:
        raise ChatHealthyException(mode="runtime_error", message=f"orchestrator: sys.argv missing 'RequestBody:' marker; "
            f"argv_count={len(sys.argv)}; joined first 500 chars={s[:500]!r}"
        )
    rest = s[idx + len(marker):].lstrip()
    try:
        body, _consumed = json.JSONDecoder().raw_decode(rest)
    except json.JSONDecodeError as e:
        raise ChatHealthyException(mode="runtime_error", message=f"orchestrator: RequestBody not parseable JSON; "
            f"argv_count={len(sys.argv)}; rest first 500 chars={rest[:500]!r}; "
            f"error={e}"
        )
    if not isinstance(body, dict):
        raise ChatHealthyException(mode="runtime_error", message=f"orchestrator: RequestBody is not a dict; got {type(body).__name__}"
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
        raise ChatHealthyException(mode="runtime_error", message=f"Start runbook {runbook!r} failed: HTTP {r.status_code} {r.text[:500]}")
    return aa_job_id


def _wake_source_cluster_early(friendly_name: str) -> None:
    """Fire-and-forget Atlas wake on the source cluster. Runs in the
    orchestrator -- the first runbook in the chain -- so Atlas resume
    runs in parallel with VM provisioning. The migrator still issues
    its own (idempotent) wake call and waits for IDLE. Best-effort:
    an exception here is logged and swallowed; the migrator retries."""
    conn_str = os.environ.get(f"MONGO_CLUSTER_{friendly_name}_connectionString")
    if not conn_str:
        log.warning("Early wake skipped: no connection string for cluster %r",
                    friendly_name)
        return
    if "@" not in conn_str:
        log.warning("Early wake skipped: connection string for %r has no '@'",
                    friendly_name)
        return
    after_at = conn_str.split("@", 1)[1]
    hostname = after_at.split("/", 1)[0].split("?", 1)[0]
    try:
        auth = HTTPDigestAuth(os.environ["ATLAS_PUBLIC_KEY"],
                              os.environ["ATLAS_PRIVATE_KEY"])
        base = (f"https://cloud.mongodb.com/api/atlas/v2/groups/"
                f"{os.environ['ATLAS_PROJECT_ID']}/clusters")
        accept = {"Accept": "application/vnd.atlas.2023-02-01+json"}
        r = requests.get(base, auth=auth, headers=accept, timeout=15)
        r.raise_for_status()
        atlas_name = None
        for c in r.json().get("results", []):
            srv = (c.get("connectionStrings", {}) or {}).get("standardSrv", "") or ""
            if hostname in srv:
                atlas_name = c["name"]
                break
        if not atlas_name:
            log.warning("Early wake skipped: no Atlas cluster matches hostname %r",
                        hostname)
            return
        requests.patch(
            f"{base}/{atlas_name}",
            json={"paused": False}, auth=auth,
            headers={**accept, "Content-Type": "application/json"},
            timeout=30,
        )
        log.info("Early Atlas wake fired on source cluster %s (atlas_name=%s)",
                 friendly_name, atlas_name)
    except Exception as e:
        log.warning("Early Atlas wake failed for %r: %r (migrator will retry)",
                    friendly_name, e)


def _main():
    global _request_guid
    payload = _read_payload()
    _request_guid = payload.get("request_guid", "?")
    if payload.get("health_check") is True:
        return
    job_id = payload.get("job_id")
    if not job_id:
        raise ChatHealthyException(mode="runtime_error", message="orchestrator: job_id missing from payload")
    vm_name = _vm_name_for_job(job_id)
    payload["vm_name"] = vm_name

    sub = os.environ["AZ_SUBSCRIPTION_ID"]
    rg = os.environ["AZ_RESOURCE_GROUP"]
    aa = os.environ["AZ_AUTOMATION_ACCOUNT"]


    src_cluster = payload.get("source_cluster")
    if src_cluster:
        _wake_source_cluster_early(src_cluster)

    provisioner_aa_job_id = _start_runbook_fire_and_forget(
        sub, rg, aa, _PROVISIONER_RUNBOOK, payload, run_on=None,
    )
    return {
        "orchestrator_status": "ok",
        "job_id": job_id,
        "vm_name": vm_name,
        "provisioner_aa_job_id": provisioner_aa_job_id,
    }


try:
    _status = _main()
    log.info(json.dumps(_status))
    sys.exit(0)
except Exception:
    tb = traceback.format_exc()
    log.error("Orchestrator failed: %s", tb)
    ChatHealthyLoggingService().info(json.dumps({"orchestrator_status": "error", "error": tb[-1500:]}))
    sys.exit(1)
