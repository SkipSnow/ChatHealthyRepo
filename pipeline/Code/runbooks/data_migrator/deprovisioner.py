"""deprovisioner.py - ChatHealthyDataMigrator deprovisioner runbook.

Runs on the standard Azure sandbox of ChatHealthyJobManager. Tears down the
ephemeral Hybrid Worker VM that the provisioner created for one migration
job, plus the role assignment provisioner created on the AA for that VM's
MI, then exits.

The actual VM destruction inside Azure is asynchronous: az REST returns
the moment the delete request is accepted. The OS disk and NIC the
provisioner created with deleteOption='Delete' get cleaned up by Azure
as part of the cascading VM delete. The role-assignment cleanup is
synchronous (cheap DELETE on the assignment resource) and runs before the
VM delete so we can still resolve the assignment id deterministically
from vm_name regardless of VM state.

Input payload (one JSON-encoded `payload` parameter, read via sys.argv[1]):
    job_id   - gateway-minted external job id (used for logging only here).
    vm_name  - VM resource name the provisioner created.

Environment (Automation Variables):
    AZ_SUBSCRIPTION_ID            - Azure subscription hosting the VM + AA.
    AZ_VM_RESOURCE_GROUP          - RG containing the VM (rg-chathealthy-pipeline-dev).
    AZ_AUTOMATION_RESOURCE_GROUP  - RG containing the AA (rg-chathealthy-pipeline-dev).
    AZ_AUTOMATION_ACCOUNT         - ChatHealthyJobManager.
"""
from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException

import base64
import json
import os
import sys
import traceback
import uuid

import requests

try:
    import automationassets
    for k in ("AZ_SUBSCRIPTION_ID", "AZ_VM_RESOURCE_GROUP",
              "AZ_AUTOMATION_RESOURCE_GROUP", "AZ_AUTOMATION_ACCOUNT"):
        try:
            os.environ[k] = str(automationassets.get_automation_variable(k))
        except Exception:
            pass
except ImportError:
    pass

log = ChatHealthyLoggingService()
_COMPUTE_API = "2024-07-01"
_NETWORK_API = "2024-01-01"
_AUTHORIZATION_API = "2022-04-01"


def _read_payload() -> dict:
    """Deprovisioner is fired by the migrator (or by the deploy health-
    check) via PUT /jobs. The sender base64-encodes the payload JSON so
    it survives legacy AA's parameter quote-stripping. sys.argv[1] is a
    base64 string."""
    if len(sys.argv) < 2:
        raise ChatHealthyException(mode="runtime_error", message="no payload: sys.argv[1] missing")
    return json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))


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
        return
    if r.status_code == 404:
        return
    raise ChatHealthyException(mode="runtime_error", message=f"VM delete REST call failed for {vm_name}: HTTP {r.status_code} {r.text[:500]}")


def _delete_nic(sub: str, vm_rg: str, vm_name: str) -> None:
    """Delete the NIC the provisioner created (deterministic name
    `{vm_name}-nic`). VM-cascade delete already removes the NIC when the
    VM exists with deleteOption='Delete' on its network attachment, but
    we delete unconditionally here for the AB-end-cleanup path where the
    provisioner created the NIC and then crashed before creating the VM
    (orphaned NIC). Idempotent on 404 (already gone)."""
    nic_name = f"{vm_name}-nic"
    token = _mi_token()
    url = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/resourceGroups/{vm_rg}/providers/Microsoft.Network"
        f"/networkInterfaces/{nic_name}?api-version={_NETWORK_API}"
    )
    r = requests.delete(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if r.status_code in (200, 202, 204):
        return
    if r.status_code == 404:
        return
    raise ChatHealthyException(mode="runtime_error", message=f"NIC delete REST call failed for {nic_name}: HTTP {r.status_code} {r.text[:500]}")


def _delete_vm_mi_role_assignment(sub: str, aa_rg: str, aa: str, vm_name: str) -> None:
    """Delete the Automation Operator role assignment the provisioner created
    on the AA for this VM's MI. Role-assignment GUID is deterministic from
    vm_name (matches provisioner._grant_vm_mi_automation_operator)."""
    assignment_guid = str(uuid.uuid5(uuid.NAMESPACE_OID, f"chdm-vm-mi-auto-op|{vm_name}"))
    aa_scope = (
        f"/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
    )
    url = (
        f"https://management.azure.com{aa_scope}"
        f"/providers/Microsoft.Authorization/roleAssignments/{assignment_guid}"
        f"?api-version={_AUTHORIZATION_API}"
    )
    token = _mi_token()
    r = requests.delete(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if r.status_code in (200, 204):
        return
    if r.status_code == 404:
        return
    raise ChatHealthyException(mode="runtime_error", message=f"DELETE role assignment {assignment_guid} on AA failed: "
        f"HTTP {r.status_code} {r.text[:500]}")


def _main():
    global _request_guid
    payload = _read_payload()
    _request_guid = payload.get("request_guid", "?")
    if payload.get("health_check") is True:
        return
    job_id = payload.get("job_id", "?")
    vm_name = payload.get("vm_name")
    if not vm_name:
        raise ChatHealthyException(mode="runtime_error", message="deprovisioner: vm_name missing from payload")

    sub = os.environ["AZ_SUBSCRIPTION_ID"]
    vm_rg = os.environ["AZ_VM_RESOURCE_GROUP"]
    aa_rg = os.environ["AZ_AUTOMATION_RESOURCE_GROUP"]
    aa = os.environ["AZ_AUTOMATION_ACCOUNT"]

    # Each cleanup step is wrapped so one failure does not block the rest
    # (AB-end safety: deprovisioning must be best-effort across all the
    # partial-state resources the provisioner may have created).
    for step_name, step_fn in (
        ("role_assignment", lambda: _delete_vm_mi_role_assignment(sub, aa_rg, aa, vm_name)),
        ("vm", lambda: _delete_vm(sub, vm_rg, vm_name)),
        ("nic", lambda: _delete_nic(sub, vm_rg, vm_name)),
    ):
        try:
            step_fn()
        except Exception as exc:
            pass
    log.info(json.dumps({"deprovisioner_status": "ok", "job_id": job_id, "vm_name": vm_name}))


try:
    _main()
    sys.exit(0)
except Exception:
    tb = traceback.format_exc()
    log.error("Deprovisioner failed: %s", tb)
    ChatHealthyLoggingService().info(json.dumps({"deprovisioner_status": "error", "error": tb[-1500:]}))
    sys.exit(1)
