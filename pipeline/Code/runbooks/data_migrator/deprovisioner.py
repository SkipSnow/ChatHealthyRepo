"""deprovisioner.py — ChatHealthyDataMigrator deprovisioner runbook.

Runs on the standard Azure sandbox of ChatHealthyJobManager. Tears down the
ephemeral Hybrid Worker VM that the provisioner created for one migration
job, then exits.

The actual VM destruction inside Azure is asynchronous: az REST returns
the moment the delete request is accepted. The OS disk and NIC the
provisioner created with deleteOption='Delete' get cleaned up by Azure
as part of the cascading VM delete.

Input payload (one JSON-encoded `payload` parameter, read via sys.argv[1]):
    job_id   — gateway-minted external job id (used for logging only here).
    vm_name  — VM resource name the provisioner created.

Environment (Automation Variables):
    AZ_SUBSCRIPTION_ID  — Azure subscription hosting the VM.
    AZ_VM_RESOURCE_GROUP — RG containing the VM (FindCareDataPipelines-Dev).
"""
import json
import logging
import os
import sys
import traceback

import requests

try:
    import automationassets
    for k in ("AZ_SUBSCRIPTION_ID", "AZ_VM_RESOURCE_GROUP"):
        try:
            os.environ[k] = str(automationassets.get_automation_variable(k))
        except Exception:
            pass
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deprovisioner")

_COMPUTE_API = "2024-07-01"


def _read_payload() -> dict:
    if len(sys.argv) < 2:
        raise RuntimeError("no payload: sys.argv[1] missing")
    return json.loads(sys.argv[1])


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


def _delete_vm(sub: str, rg: str, vm_name: str) -> None:
    token = _mi_token()
    url = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/resourceGroups/{rg}/providers/Microsoft.Compute"
        f"/virtualMachines/{vm_name}?api-version={_COMPUTE_API}"
    )
    r = requests.delete(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if r.status_code in (200, 202, 204):
        log.info("VM delete accepted: %s (status=%d)", vm_name, r.status_code)
        return
    if r.status_code == 404:
        log.info("VM not found (already gone): %s", vm_name)
        return
    raise RuntimeError(
        f"VM delete REST call failed for {vm_name}: HTTP {r.status_code} {r.text[:500]}"
    )


def _main():
    payload = _read_payload()
    job_id = payload.get("job_id", "?")
    vm_name = payload.get("vm_name")
    if not vm_name:
        raise RuntimeError("deprovisioner: vm_name missing from payload")

    sub = os.environ["AZ_SUBSCRIPTION_ID"]
    rg = os.environ["AZ_VM_RESOURCE_GROUP"]

    log.info("Deprovision begin: job_id=%s vm_name=%s rg=%s", job_id, vm_name, rg)
    _delete_vm(sub, rg, vm_name)
    log.info("Deprovision complete: job_id=%s vm_name=%s (delete is async in Azure)",
             job_id, vm_name)
    print(json.dumps({"deprovisioner_status": "ok", "job_id": job_id, "vm_name": vm_name}),
          flush=True)


try:
    _main()
    sys.exit(0)
except Exception:
    tb = traceback.format_exc()
    log.error("Deprovisioner failed: %s", tb)
    print(json.dumps({"deprovisioner_status": "error", "error": tb[-1500:]}), flush=True)
    sys.exit(1)
