"""provisioner.py — ChatHealthyDataMigrator provisioner runbook.

Runs on the standard Azure sandbox of ChatHealthyJobManager. Stands up
the ephemeral Hybrid Worker VM that the migrator runbook will execute
on. The orchestrator runbook calls this synchronously (waits for the
Automation job to reach Completed/Failed before continuing).

Sequence:
  1. Create a NIC in ChatHealthy-VNet's undelegated subnet
     (hybrid-worker-subnet, 10.0.2.0/24). NIC deleteOption=Delete so
     it cascades on VM delete.
  2. Create the VM:
       - Name      : <vm_name> (computed by orchestrator from job_id).
       - SKU       : Standard_D16s_v5 (16 vCPU / 64 GB / accelerated net).
       - Image     : Ubuntu 22.04 LTS (Canonical:0001-com-ubuntu-server-jammy).
       - VNet      : ChatHealthy-VNet (eastus2).
       - Auth      : system-assigned managed identity.
       - OS disk   : deleteOption=Delete (cascades on VM delete).
       - cloud-init: installs python3-pip, pymongo, dnspython, requests.
  3. Wait for VM provisioning to reach Succeeded.
  4. Install the Hybrid Worker extension (HybridWorkerForLinux). The
     extension settings carry the Automation Account JRDS URL and the
     Hybrid Worker Group name; once installed the worker registers
     automatically with that group.
  5. Wait for extension provisioning to reach Succeeded.

Input parameters:
    job_id  — gateway-minted external job id (used only for logging).
    vm_name — deterministic VM name minted by the orchestrator.

Environment (Automation Variables):
    AZ_SUBSCRIPTION_ID       — Azure subscription hosting the VM.
    AZ_VM_RESOURCE_GROUP     — RG to create the VM in (FindCareDataPipelines-Dev).
    AZ_VM_LOCATION           — Azure region (eastus2).
    AZ_VM_SIZE               — VM SKU (Standard_D16s_v5).
    AZ_VM_SUBNET_ID          — ARM resource id of the undelegated subnet
                               (.../ChatHealthy-VNet/subnets/hybrid-worker-subnet).
    AZ_VM_ADMIN_USERNAME     — Linux admin username for the VM.
    AZ_VM_ADMIN_SSH_PUBKEY   — Public SSH key for the admin user.
    AZ_AUTOMATION_RESOURCE_GROUP — RG of the Automation Account.
    AZ_AUTOMATION_ACCOUNT        — ChatHealthyJobManager.
    AZ_AUTOMATION_JRDS_URL       — Job Runtime Data Service URL for the AA
                                   (region-specific; e.g.
                                   https://eus2-jobruntimedata-prod-su1.azure-automation.net).
"""
import base64
import json
import logging
import os
import sys
import time
import traceback

import requests

try:
    import automationassets
    for k in ("AZ_SUBSCRIPTION_ID", "AZ_VM_RESOURCE_GROUP", "AZ_VM_LOCATION",
              "AZ_VM_SIZE", "AZ_VM_SUBNET_ID", "AZ_VM_ADMIN_USERNAME",
              "AZ_VM_ADMIN_SSH_PUBKEY",
              "AZ_AUTOMATION_RESOURCE_GROUP", "AZ_AUTOMATION_ACCOUNT",
              "AZ_AUTOMATION_JRDS_URL"):
        try:
            os.environ[k] = str(automationassets.get_automation_variable(k))
        except Exception:
            pass
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("provisioner")

_COMPUTE_API = "2024-07-01"
_NETWORK_API = "2024-01-01"
_HYBRID_WORKER_GROUP = "ChatHealthyDataMigratorWorkGroup"
_WAIT_POLL_SEC = 10
_VM_WAIT_TIMEOUT_SEC = 15 * 60
_EXT_WAIT_TIMEOUT_SEC = 10 * 60

# Ubuntu 22.04 LTS (Jammy). Canonical's stable LTS image for Azure.
_IMAGE_REFERENCE = {
    "publisher": "Canonical",
    "offer":     "0001-com-ubuntu-server-jammy",
    "sku":       "22_04-lts",
    "version":   "latest",
}

# cloud-init userdata: install Python deps the migrator runbook needs once
# the Hybrid Worker extension begins dispatching jobs to it. Runs on first
# boot before the Hybrid Worker extension's runtime starts the runbook.
_CLOUD_INIT = """#cloud-config
package_update: true
packages:
  - python3-pip
runcmd:
  - [ pip3, install, --no-cache-dir, pymongo, dnspython, requests ]
"""


def _get_param(name: str, webhook_data, job_params) -> str | None:
    if webhook_data:
        try:
            body = json.loads(webhook_data.get("RequestBody", "{}"))
            v = body.get(name)
            if v is not None:
                return str(v)
        except Exception:
            pass
    if job_params and name in job_params:
        return str(job_params[name])
    return None


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
               nic_id: str, admin_username: str, ssh_pubkey: str) -> None:
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
        f"?api-version={_COMPUTE_API}"
    )
    custom_data = base64.b64encode(_CLOUD_INIT.encode("utf-8")).decode("ascii")
    body = {
        "location": location,
        "identity": {"type": "SystemAssigned"},
        "properties": {
            "hardwareProfile": {"vmSize": vm_size},
            "storageProfile": {
                "imageReference": _IMAGE_REFERENCE,
                "osDisk": {
                    "createOption": "FromImage",
                    "managedDisk": {"storageAccountType": "Premium_LRS"},
                    "deleteOption": "Delete",
                },
            },
            "osProfile": {
                "computerName": vm_name,
                "adminUsername": admin_username,
                "customData": custom_data,
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


def _install_hybrid_worker_extension(sub: str, rg: str, vm_name: str,
                                     jrds_url: str, aa_rg: str, aa: str) -> None:
    """Install the Linux Hybrid Worker extension. Once running, the extension
    registers a worker into the named Hybrid Worker group automatically.

    Settings shape per Microsoft docs for the extension-based Hybrid Worker
    onboarding flow: the extension reads AutomationAccountURL (the JRDS URL
    for the AA's region) and the target Hybrid Worker Group name from the
    extension settings; it self-registers using the VM's managed identity.
    """
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
        f"/extensions/HybridWorkerExtension?api-version={_COMPUTE_API}"
    )
    automation_account_url = (
        f"{jrds_url.rstrip('/')}/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/automationAccounts/{aa}"
    )
    body = {
        "location": os.environ["AZ_VM_LOCATION"],
        "properties": {
            "publisher": "Microsoft.Azure.Automation.HybridWorker",
            "type": "HybridWorkerForLinux",
            "typeHandlerVersion": "1.1",
            "autoUpgradeMinorVersion": True,
            "settings": {
                "AutomationAccountURL": automation_account_url,
            },
        },
    }
    log.info("Installing Hybrid Worker extension on %s; AA URL=%s",
             vm_name, automation_account_url)
    _arm_put(url, body)
    _wait_provisioning(url, _EXT_WAIT_TIMEOUT_SEC,
                       what=f"HybridWorkerExtension on {vm_name}")


def _register_hybrid_worker(sub: str, aa_rg: str, aa: str, vm_id: str,
                            worker_name: str) -> None:
    """Register the VM as a Hybrid Worker in ChatHealthyDataMigratorWorkGroup.

    The extension-based onboarding flow expects a worker resource to exist
    on the Automation Account side before the extension can attach. This
    PUT creates the worker resource pointing back at the VM's ARM id.
    """
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/hybridRunbookWorkerGroups/{_HYBRID_WORKER_GROUP}"
        f"/hybridRunbookWorkers/{worker_name}?api-version=2023-11-01"
    )
    body = {
        "properties": {
            "vmResourceId": vm_id,
        },
    }
    log.info("Registering Hybrid Worker %s -> %s in group %s",
             worker_name, vm_id, _HYBRID_WORKER_GROUP)
    _arm_put(url, body)


def _vm_resource_id(sub: str, rg: str, vm_name: str) -> str:
    return (
        f"/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
    )


def _main(webhook_data, job_params):
    job_id = _get_param("job_id", webhook_data, job_params)
    vm_name = _get_param("vm_name", webhook_data, job_params)
    if not job_id or not vm_name:
        raise RuntimeError("provisioner: job_id and vm_name parameters are required")

    sub = os.environ["AZ_SUBSCRIPTION_ID"]
    vm_rg = os.environ["AZ_VM_RESOURCE_GROUP"]
    location = os.environ["AZ_VM_LOCATION"]
    vm_size = os.environ["AZ_VM_SIZE"]
    subnet_id = os.environ["AZ_VM_SUBNET_ID"]
    admin_user = os.environ["AZ_VM_ADMIN_USERNAME"]
    ssh_pubkey = os.environ["AZ_VM_ADMIN_SSH_PUBKEY"]
    aa_rg = os.environ["AZ_AUTOMATION_RESOURCE_GROUP"]
    aa = os.environ["AZ_AUTOMATION_ACCOUNT"]
    jrds_url = os.environ["AZ_AUTOMATION_JRDS_URL"]

    log.info("Provision begin: job_id=%s vm_name=%s size=%s", job_id, vm_name, vm_size)

    nic_id = _create_nic(sub, vm_rg, location, vm_name, subnet_id)
    _create_vm(sub, vm_rg, location, vm_name, vm_size, nic_id, admin_user, ssh_pubkey)

    vm_id = _vm_resource_id(sub, vm_rg, vm_name)
    _register_hybrid_worker(sub, aa_rg, aa, vm_id, worker_name=vm_name)
    _install_hybrid_worker_extension(sub, vm_rg, vm_name, jrds_url, aa_rg, aa)

    log.info("Provision complete: vm_name=%s registered in %s",
             vm_name, _HYBRID_WORKER_GROUP)
    print(json.dumps({
        "provisioner_status": "ok",
        "job_id": job_id,
        "vm_name": vm_name,
        "vm_id": vm_id,
        "hybrid_worker_group": _HYBRID_WORKER_GROUP,
    }), flush=True)


try:
    _wd = None
    try:
        _wd = WebhookData  # noqa: F821
    except NameError:
        pass
    _main(_wd, None)
    sys.exit(0)
except Exception:
    tb = traceback.format_exc()
    log.error("Provisioner failed: %s", tb)
    print(json.dumps({"provisioner_status": "error", "error": tb[-1500:]}), flush=True)
    sys.exit(1)
