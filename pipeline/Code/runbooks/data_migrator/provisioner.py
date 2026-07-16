"""provisioner.py - ChatHealthyDataMigrator provisioner runbook.

Runs on the standard Azure sandbox of ChatHealthyJobManager. Stands up
the ephemeral Hybrid Worker VM, then fires the migrator runbook on the
Hybrid Worker group as the last step and exits. Chain-fire pattern; no
wait on the migrator job's completion.

Sequence:
  1. Create a NIC in ChatHealthy-VNet's undelegated subnet. Wait for
     provisioningState=Succeeded (approved poll #2).
  2. Create the VM (Standard_D16s_v5 / Ubuntu 22.04 LTS / Standard_LRS
     OS disk / system-assigned managed identity / SSH-key admin).
     Wait for provisioningState=Succeeded (approved poll #1).
  3. PUT a hybridRunbookWorker resource on the Automation Account
     (worker name is a freshly-minted GUID; vmResourceId references the
     VM's ARM id).
  4. GET the Automation Account and extract its
     properties.automationHybridServiceUrl.
  5. PUT the HybridWorkerForLinux extension on the VM with that URL in
     settings.AutomationAccountURL. No wait - the AA will queue the
     migrator job until the worker finishes registering.
  6. Fire the migrator runbook (run_on=ChatHealthyHybridWorkers)
     fire-and-forget. Log the migrator AA job_id and exit.

Input payload (one JSON-encoded `payload` parameter, read via sys.argv[1]):
    job_id, vm_name, plus every migration arg the orchestrator forwarded.

Environment (Automation Variables):
    AZ_SUBSCRIPTION_ID
    AZ_VM_RESOURCE_GROUP
    AZ_VM_LOCATION
    AZ_VM_SIZE
    AZ_VM_SUBNET_ID
    AZ_VM_ADMIN_USERNAME
    AZ_VM_ADMIN_SSH_PUBKEY
    AZ_AUTOMATION_RESOURCE_GROUP
    AZ_AUTOMATION_ACCOUNT
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
import traceback
import uuid

import requests

try:
    import automationassets
    for k in ("AZ_SUBSCRIPTION_ID", "AZ_VM_RESOURCE_GROUP", "AZ_VM_LOCATION",
              "AZ_VM_SIZE", "AZ_VM_SUBNET_ID", "AZ_VM_ADMIN_USERNAME",
              "AZ_VM_ADMIN_SSH_PUBKEY",
              "AZ_AUTOMATION_RESOURCE_GROUP", "AZ_AUTOMATION_ACCOUNT"):
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
log = logging.getLogger("provisioner")
log.addFilter(_RGFilter())

_COMPUTE_API = "2024-07-01"
_NETWORK_API = "2024-01-01"
_AUTOMATION_API = "2023-11-01"
_AUTHORIZATION_API = "2022-04-01"
_HYBRID_WORKER_API = "2024-10-23"
_HYBRID_WORKER_GROUP = "ChatHealthyHybridWorkers"
_MIGRATOR_RUNBOOK = "ChatHealthyDataMigrator"
_WAIT_POLL_SEC = 10
_VM_WAIT_TIMEOUT_SEC = 20 * 60

# Automation Operator built-in role definition GUID. Lets the principal start
# runbook jobs on the Automation Account. The migrator runs on the HW VM with
# the VM's system MI; without this role it gets 403 when firing the
# deprovisioner.
_AUTOMATION_OPERATOR_ROLE_DEF_GUID = "d3881f73-407a-4167-8283-e981cbba0404"

_IMAGE_REFERENCE = {
    "publisher": "Canonical",
    "offer":     "0001-com-ubuntu-server-jammy",
    "sku":       "22_04-lts",
    "version":   "latest",
}

# Installs pymongo/dnspython/requests on the fresh Hybrid Worker VM.
# Runs synchronously as root; the provisioner waits for provisioningState
# before installing the Hybrid Worker extension.
# `set -o pipefail` is required so failures of cmd inside `cmd | tee` are
# not masked by tee's exit code.
_PYTHON_DEPS_INSTALL = (
    "set -o pipefail; "
    "LOG=/var/log/chdm_python_deps_install.log; "
    "echo === chdm python deps install start at $(date -u) === | tee -a \"$LOG\"; "
    "echo --- waiting for cloud-init --- | tee -a \"$LOG\"; "
    "timeout 300 cloud-init status --wait --long 2>&1 | tee -a \"$LOG\" || echo cloud-init wait non-zero, continuing | tee -a \"$LOG\"; "
    "echo --- waiting for dpkg lock --- | tee -a \"$LOG\"; "
    "for i in 1 2 3 4 5 6; do fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || break; echo dpkg lock held, sleep 20 | tee -a \"$LOG\"; sleep 20; done; "
    "apt_ok=0; "
    "for i in 1 2 3 4; do "
    "  echo --- attempt $i apt --- | tee -a \"$LOG\"; "
    "  if apt-get update -y 2>&1 | tee -a \"$LOG\" && apt-get install -y python3-pip 2>&1 | tee -a \"$LOG\"; then apt_ok=1; break; fi; "
    "  echo attempt $i apt FAILED, sleep $((10*i))s | tee -a \"$LOG\"; sleep $((10*i)); "
    "done; "
    "if [ \"$apt_ok\" != \"1\" ]; then echo FATAL apt phase failed; tail -c 3500 \"$LOG\" >&2; exit 1; fi; "
    "pip_ok=0; "
    "for i in 1 2 3 4; do "
    "  echo --- attempt $i pip --- | tee -a \"$LOG\"; "
    "  if pip3 install --no-cache-dir --retries 3 --timeout 30 pymongo dnspython requests 2>&1 | tee -a \"$LOG\"; then pip_ok=1; break; fi; "
    "  echo attempt $i pip FAILED, sleep $((10*i))s | tee -a \"$LOG\"; sleep $((10*i)); "
    "done; "
    "if [ \"$pip_ok\" != \"1\" ]; then echo FATAL pip phase failed; tail -c 3500 \"$LOG\" >&2; exit 1; fi; "
    "echo === chdm python deps install success at $(date -u) === | tee -a \"$LOG\"; "
    "exit 0"
)


def _read_payload() -> dict:
    """Provisioner is fired via PUT /jobs (orchestrator does this, deploy
    health-check does this). The sender base64-encodes the payload JSON
    so it survives legacy AA's quote-stripping parameter handling.
    sys.argv[1] is a base64 string; decode + json.loads."""
    if len(sys.argv) < 2:
        raise RuntimeError("no payload: sys.argv[1] missing")
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


def _arm_put(url: str, body: dict) -> dict:
    token = _mi_token()
    r = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body, timeout=120,
    )
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"ARM PUT failed {r.status_code}: {r.text[:600]}\nurl={url}")
    return r.json() if r.text else {}


def _arm_get(url: str) -> dict:
    token = _mi_token()
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def _wait_provisioning(url: str, deadline_sec: float, what: str) -> None:
    deadline = time.time() + deadline_sec
    while time.time() < deadline:
        body = _arm_get(url)
        state = body.get("properties", {}).get("provisioningState", "")
        log.info("%s provisioningState=%s", what, state)
        if state == "Succeeded":
            return
        if state in ("Failed", "Canceled"):
            raise RuntimeError(f"{what} provisioningState={state}: {body}")
        time.sleep(_WAIT_POLL_SEC)
    raise RuntimeError(f"{what} did not reach Succeeded within {deadline_sec}s")


def _create_nic(sub: str, rg: str, location: str, vm_name: str, subnet_id: str) -> str:
    nic_name = f"{vm_name}-nic"
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Network/networkInterfaces/{nic_name}"
        f"?api-version={_NETWORK_API}"
    )
    body = {
        "location": location,
        "properties": {
            "enableAcceleratedNetworking": True,
            "ipConfigurations": [{
                "name": "ipconfig1",
                "properties": {
                    "subnet": {"id": subnet_id},
                    "privateIPAllocationMethod": "Dynamic",
                },
            }],
        },
    }
    log.info("Creating NIC %s in %s", nic_name, subnet_id)
    _arm_put(url, body)
    _wait_provisioning(url, _VM_WAIT_TIMEOUT_SEC, what=f"NIC {nic_name}")
    nic = _arm_get(url)
    return nic["id"]


def _create_vm(sub: str, rg: str, location: str, vm_name: str, vm_size: str,
               nic_id: str, admin_username: str, ssh_pubkey: str) -> str:
    """Create the VM. Returns its system-assigned MI principalId. Does NOT
    use cloud-init for python deps - python deps are installed
    synchronously via the separate CustomScript extension (see
    _install_python_deps_extension) so the HW extension cannot race them."""
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
        f"?api-version={_COMPUTE_API}"
    )
    body = {
        "location": location,
        "identity": {"type": "SystemAssigned"},
        "properties": {
            "hardwareProfile": {"vmSize": vm_size},
            "storageProfile": {
                "imageReference": _IMAGE_REFERENCE,
                "osDisk": {
                    "createOption": "FromImage",
                    "managedDisk": {"storageAccountType": "Standard_LRS"},
                    "deleteOption": "Delete",
                },
            },
            "osProfile": {
                "computerName": vm_name,
                "adminUsername": admin_username,
                "linuxConfiguration": {
                    "disablePasswordAuthentication": True,
                    "ssh": {
                        "publicKeys": [{
                            "path": f"/home/{admin_username}/.ssh/authorized_keys",
                            "keyData": ssh_pubkey,
                        }],
                    },
                },
            },
            "networkProfile": {
                "networkInterfaces": [{
                    "id": nic_id,
                    "properties": {"deleteOption": "Delete"},
                }],
            },
        },
    }
    log.info("Creating VM %s size=%s", vm_name, vm_size)
    _arm_put(url, body)
    _wait_provisioning(url, _VM_WAIT_TIMEOUT_SEC, what=f"VM {vm_name}")
    vm_body = _arm_get(url)
    principal_id = vm_body.get("identity", {}).get("principalId")
    if not principal_id:
        raise RuntimeError(
            f"VM {vm_name!r} has no identity.principalId after provisioning; "
            f"the system-assigned MI did not materialize."
        )
    return principal_id


def _install_python_deps_extension(sub: str, vm_rg: str, vm_name: str,
                                   location: str) -> None:
    """Install pymongo/dnspython/requests via CustomScript extension and
    wait for provisioningState=Succeeded. Synchronous on purpose: the HW
    extension cannot start the migrator until python deps are present."""
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{vm_rg}"
        f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
        f"/extensions/InstallPythonDeps?api-version={_COMPUTE_API}"
    )
    body = {
        "location": location,
        "properties": {
            "publisher": "Microsoft.Azure.Extensions",
            "type": "CustomScript",
            "typeHandlerVersion": "2.1",
            "autoUpgradeMinorVersion": True,
            "settings": {},
            "protectedSettings": {
                # bash -c '...' (single-quoted outside) so the script can
                # use double quotes around shell variable expansions like
                # "$LOG" without escaping. The script itself must not
                # contain literal single quotes.
                "commandToExecute": f"bash -c '{_PYTHON_DEPS_INSTALL}'",
            },
        },
    }
    log.info("Installing python deps via CustomScript extension on %s (wait)", vm_name)
    _arm_put(url, body)
    _wait_provisioning(url, _VM_WAIT_TIMEOUT_SEC, what=f"CustomScript {vm_name}")


def _grant_vm_mi_automation_operator(sub: str, aa_rg: str, aa: str,
                                     vm_principal_id: str, vm_name: str) -> str:
    """PUT a role assignment so the VM's MI can start runbooks on the AA.
    Without this the migrator (running on the VM) gets 403 when it fires
    the deprovisioner. Role-assignment GUID is deterministic from vm_name so
    the deprovisioner can DELETE the same one without coordination."""
    role_def_id = (
        f"/subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions"
        f"/{_AUTOMATION_OPERATOR_ROLE_DEF_GUID}"
    )
    aa_scope = (
        f"/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
    )
    assignment_guid = str(uuid.uuid5(uuid.NAMESPACE_OID, f"chdm-vm-mi-auto-op|{vm_name}"))
    url = (
        f"https://management.azure.com{aa_scope}"
        f"/providers/Microsoft.Authorization/roleAssignments/{assignment_guid}"
        f"?api-version={_AUTHORIZATION_API}"
    )
    body = {
        "properties": {
            "roleDefinitionId": role_def_id,
            "principalId": vm_principal_id,
            "principalType": "ServicePrincipal",
        }
    }
    log.info("Granting VM MI Automation Operator on AA (assignment=%s)", assignment_guid)
    _arm_put(url, body)
    return assignment_guid


def _register_hybrid_worker(sub: str, aa_rg: str, aa: str, vm_id: str) -> str:
    """Create the hybrid worker resource on the AA, then return its name
    (a freshly-minted GUID). The GUID is opaque to operators; the binding
    to the VM is via vmResourceId in the body, per Microsoft Learn."""
    worker_guid = str(uuid.uuid4())
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/hybridRunbookWorkerGroups/{_HYBRID_WORKER_GROUP}"
        f"/hybridRunbookWorkers/{worker_guid}?api-version={_HYBRID_WORKER_API}"
    )
    body = {"properties": {"vmResourceId": vm_id}}
    log.info("Registering Hybrid Worker %s -> %s in group %s",
             worker_guid, vm_id, _HYBRID_WORKER_GROUP)
    _arm_put(url, body)
    return worker_guid


def _get_automation_hybrid_service_url(sub: str, aa_rg: str, aa: str) -> str:
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"?api-version={_AUTOMATION_API}"
    )
    body = _arm_get(url)
    svc_url = body.get("properties", {}).get("automationHybridServiceUrl")
    if not svc_url:
        raise RuntimeError(
            f"AA {aa!r} has no properties.automationHybridServiceUrl in its GET response; "
            f"cannot wire the Hybrid Worker extension."
        )
    return svc_url


def _install_hybrid_worker_extension(sub: str, vm_rg: str, vm_name: str,
                                     automation_hybrid_service_url: str,
                                     location: str) -> None:
    """PUT the HybridWorkerForLinux extension. Do NOT wait for
    provisioningState - the migrator job will sit Queued on the
    Automation Account until the worker registers, which is the AA's
    natural flow-control."""
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{vm_rg}"
        f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
        f"/extensions/HybridWorkerExtension?api-version={_COMPUTE_API}"
    )
    body = {
        "location": location,
        "properties": {
            "publisher": "Microsoft.Azure.Automation.HybridWorker",
            "type": "HybridWorkerForLinux",
            "typeHandlerVersion": "1.1",
            "autoUpgradeMinorVersion": True,
            "enableAutomaticUpgrade": True,
            "settings": {
                "AutomationAccountURL": automation_hybrid_service_url,
            },
        },
    }
    log.info("Installing Hybrid Worker extension on %s (no wait)", vm_name)
    _arm_put(url, body)


def _fire_migrator(sub: str, aa_rg: str, aa: str, payload: dict) -> str:
    """Start the migrator runbook on ChatHealthyHybridWorkers,
    fire-and-forget. Returns the AA job_id."""
    aa_job_id = str(uuid.uuid4())
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/jobs/{aa_job_id}?api-version={_AUTOMATION_API}"
    )
    # Base64-encode the payload JSON so it survives legacy AA's
    # parameter quote-stripping (see _read_payload note in receiving runbook).
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    body = {
        "properties": {
            "runbook": {"name": _MIGRATOR_RUNBOOK},
            "parameters": {"payload": encoded},
            "runOn": _HYBRID_WORKER_GROUP,
        }
    }
    token = _mi_token()
    r = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body, timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"Start migrator failed: HTTP {r.status_code} {r.text[:500]}"
        )
    return aa_job_id


def _vm_resource_id(sub: str, rg: str, vm_name: str) -> str:
    return (
        f"/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
    )


_DEPROVISIONER_RUNBOOK = "ChatHealthyDataMigratorDeprovisioner"


def _fire_deprovisioner_on_failure(sub: str, aa_rg: str, aa: str,
                                    job_id: str, vm_name: str) -> str:
    """AB-end safety: if the provisioner fails after creating any Azure
    resource (NIC, VM, MI grant, HW worker, extensions), fire the
    deprovisioner to clean up the partial state so we don't leak. The
    deprovisioner is idempotent on missing resources (DELETE returns 404
    -> treated as already-gone). Returns the deprovisioner's AA job_id."""
    aa_job_id = str(uuid.uuid4())
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/jobs/{aa_job_id}?api-version={_AUTOMATION_API}"
    )
    encoded = base64.b64encode(json.dumps({
        "job_id": job_id, "vm_name": vm_name, "request_guid": _request_guid,
    }).encode("utf-8")).decode("ascii")
    body = {
        "properties": {
            "runbook": {"name": _DEPROVISIONER_RUNBOOK},
            "parameters": {"payload": encoded},
        }
    }
    token = _mi_token()
    r = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body, timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"Start deprovisioner-on-failure failed: HTTP {r.status_code} {r.text[:500]}"
        )
    return aa_job_id


def _main():
    global _request_guid
    payload = _read_payload()
    _request_guid = payload.get("request_guid", "?")
    if payload.get("health_check") is True:
        log.info("health_check fired on provisioner runbook")
        return
    job_id = payload.get("job_id")
    vm_name = payload.get("vm_name")
    if not job_id or not vm_name:
        raise RuntimeError("provisioner: job_id and vm_name are required in the payload")

    sub = os.environ["AZ_SUBSCRIPTION_ID"]
    vm_rg = os.environ["AZ_VM_RESOURCE_GROUP"]
    location = os.environ["AZ_VM_LOCATION"]
    # TEMPORARY 2026-06-03: contract slide 6 calls for Standard_D16s_v5
    # but the subscription's Standard DSv5 Family quota in eastus2 is 0
    # AND its Total Regional vCPUs limit is 10. Force-override the VM size
    # to Standard_D8s_v3 (8 cores, Dsv3 family - both that family and the
    # regional total have a 10-core limit with current usage 0). REVERT
    # to `vm_size = os.environ["AZ_VM_SIZE"]` once the DSv5 quota grant
    # lands. Tracked in operator chat 2026-06-03.
    vm_size = "Standard_D8s_v3"
    subnet_id = os.environ["AZ_VM_SUBNET_ID"]
    admin_user = os.environ["AZ_VM_ADMIN_USERNAME"]
    ssh_pubkey = os.environ["AZ_VM_ADMIN_SSH_PUBKEY"]
    aa_rg = os.environ["AZ_AUTOMATION_RESOURCE_GROUP"]
    aa = os.environ["AZ_AUTOMATION_ACCOUNT"]

    log.info("Provision begin: job_id=%s vm_name=%s size=%s", job_id, vm_name, vm_size)

    # AB-end safety per slide 4: from the first resource creation onward,
    # any exception triggers the deprovisioner to clean up partial state.
    # Without this, a mid-flow failure (e.g. VM-create rejected because the
    # subscription's Microsoft.Compute provider isn't registered) leaks
    # the NIC the provisioner just created. The deprovisioner is
    # idempotent on missing resources (DELETE 404 -> already-gone).
    try:
        nic_id = _create_nic(sub, vm_rg, location, vm_name, subnet_id)
        vm_principal_id = _create_vm(sub, vm_rg, location, vm_name, vm_size,
                                     nic_id, admin_user, ssh_pubkey)
        # Install python deps synchronously BEFORE the HW extension so the worker
        # cannot dequeue the migrator job until pymongo/dnspython exist.
        _install_python_deps_extension(sub, vm_rg, vm_name, location)
        # Grant the VM's MI start-runbook rights on the AA so the migrator can
        # fire the deprovisioner back on the AA without 403.
        role_assignment_guid = _grant_vm_mi_automation_operator(
            sub, aa_rg, aa, vm_principal_id, vm_name,
        )

        vm_id = _vm_resource_id(sub, vm_rg, vm_name)
        worker_guid = _register_hybrid_worker(sub, aa_rg, aa, vm_id)
        automation_hybrid_service_url = _get_automation_hybrid_service_url(sub, aa_rg, aa)
        _install_hybrid_worker_extension(sub, vm_rg, vm_name,
                                         automation_hybrid_service_url, location)

        log.info("Provision complete: vm_name=%s worker_guid=%s; firing migrator on %s",
                 vm_name, worker_guid, _HYBRID_WORKER_GROUP)
        migrator_aa_job_id = _fire_migrator(sub, aa_rg, aa, payload)
    except Exception as exc:
        log.error("Provisioner FAILED mid-flow; firing deprovisioner to clean partial state: %r", exc)
        try:
            cleanup_aa_job_id = _fire_deprovisioner_on_failure(
                sub, aa_rg, aa, job_id, vm_name,
            )
            log.info("Provisioner fired cleanup deprovisioner: job_id=%s deprovisioner_aa_job_id=%s",
                     job_id, cleanup_aa_job_id)
        except Exception as cleanup_exc:
            log.error("CLEANUP DEPROVISIONER FIRE FAILED; partial state will leak: %r", cleanup_exc)
        raise
    log.info("Provisioner fired migrator: job_id=%s migrator_aa_job_id=%s",
             job_id, migrator_aa_job_id)
    print(json.dumps({
        "provisioner_status": "ok",
        "job_id": job_id,
        "vm_name": vm_name,
        "vm_id": vm_id,
        "worker_guid": worker_guid,
        "vm_mi_principal_id": vm_principal_id,
        "role_assignment_guid": role_assignment_guid,
        "hybrid_worker_group": _HYBRID_WORKER_GROUP,
        "migrator_aa_job_id": migrator_aa_job_id,
    }), flush=True)


try:
    _main()
    sys.exit(0)
except Exception:
    tb = traceback.format_exc()
    log.error("Provisioner failed: %s", tb)
    print(json.dumps({"provisioner_status": "error", "error": tb[-1500:]}), flush=True)
    sys.exit(1)
