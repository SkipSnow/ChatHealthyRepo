"""worker_vm_provisioning.py - provision a Worker VM for the two-VM run shape.

The two-VM run (see pipeline_run_compute_deployment diagram and the pipeline LLD
"Two-VM Run Compute Topology" section) splits a run across a small Controller VM
and N big Worker VMs. The Controller enqueues one work_item per partition onto
the always-on front-end cluster exactly as it does for the single-VM shape, then
- instead of spawning local worker subprocesses - provisions one Worker VM per
replica. Each Worker VM boots this cloud-init, which runs pipeline_worker.py:
the worker atomically claims one already-enqueued work_item, does the step's
work, exits, and the VM self-deletes. The Controller never holds a handle on a
Worker VM; it watches the work_items heartbeats, the same substrate it already
uses for subprocess workers.

This path is reached only when a pipeline's config declares worker_compute='vm';
every current pipeline (provider included) leaves it unset and keeps the
subprocess path untouched. The Controller authenticates to Azure ARM as
pipelineController (whose rights include creating the Worker VMs); the Worker
VMs carry pipelineEditor credentials for their own Mongo certificate auth,
exactly as the single-VM workers do today.

The ARM shape mirrors the trigger runbook's run-VM provisioning. The runbook is a
standalone Automation module that carries no pipeline package and cannot import
this one, so the two provisioners are deliberately separate rather than shared.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

_log = ChatHealthyLoggingService()

_ARM = "https://management.azure.com"
_VM_API = "2024-03-01"
_NIC_API = "2023-09-01"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _controller_arm_token() -> str:
    """An ARM token obtained as pipelineController, the identity the Controller
    runs under. Mirrors the runbook's client-credentials flow, reading the
    pipelineController service-principal credentials from the Controller VM's
    environment. A missing credential aborts rather than falling back to any
    ambient identity."""
    tenant = _env("PIPELINECONTROLLER_AZURE_TENANT_ID")
    client = _env("PIPELINECONTROLLER_AZURE_CLIENT_ID")
    secret = _env("PIPELINECONTROLLER_AZURE_CLIENT_SECRET")
    missing = [n for n, v in (
        ("PIPELINECONTROLLER_AZURE_TENANT_ID", tenant),
        ("PIPELINECONTROLLER_AZURE_CLIENT_ID", client),
        ("PIPELINECONTROLLER_AZURE_CLIENT_SECRET", secret)) if not v]
    if missing:
        raise ChatHealthyException(
            mode="identity_credential_absent",
            message="cannot provision Worker VMs as pipelineController: "
                    + ", ".join(missing) + " absent from the Controller environment",
            component="worker_vm_provisioning",
            missing=",".join(missing))
    body = (f"grant_type=client_credentials&client_id={client}"
            f"&client_secret={secret}"
            f"&scope={_ARM}/.default").encode("utf-8")
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))["access_token"]


def _put(url: str, payload: dict, token: str) -> dict:
    req = urllib.request.Request(
        url, method="PUT",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1500]
        except Exception:  # noqa: BLE001
            detail = ""
        raise ChatHealthyException(
            mode="runtime_error",
            message=f"ARM PUT {url.rsplit('?', 1)[0]} -> HTTP {exc.code}: {detail}",
            component="worker_vm_provisioning",
            exception=exc) from exc


def _worker_cloud_init(run_id: str, step: str, replica: int) -> str:
    """Base64 cloud-init for a Worker VM: pull the pipeline image, run
    pipeline_worker.py once (it claims one work_item for this run+step), then
    self-delete the VM. pipelineEditor is the identity the worker presents to
    Azure (ACR pull, farewell delete) and to Mongo (cert auth), exactly as the
    single-VM workers do."""
    acr = _env("AUTOMATION_VM_ACR", "chpipelinedevacr")
    repo = _env("AUTOMATION_VM_IMAGE_REPO", "pipeline-control")
    tag = _env("AUTOMATION_VM_IMAGE_TAG", "latest")
    image = f"{acr}.azurecr.io/{repo}:{tag}"
    sub = _env("AZURE_SUBSCRIPTION_ID")
    rg = _env("AZURE_RESOURCE_GROUP")
    vm_name = f"vm-chworker-{_short(run_id)}-{step}-{replica}"[:63]
    pe_tenant = _env("PIPELINEEDITOR_AZURE_TENANT_ID")
    pe_client = _env("PIPELINEEDITOR_AZURE_CLIENT_ID")
    pe_secret = _env("PIPELINEEDITOR_AZURE_CLIENT_SECRET")
    yaml_body = f"""#cloud-config
apt:
  primary:
    - arches: [default]
      uri: https://archive.ubuntu.com/ubuntu
  security:
    - arches: [default]
      uri: https://security.ubuntu.com/ubuntu
package_update: true
packages:
  - docker.io
runcmd:
  - |
    #!/bin/bash
    set -eux
    exec >> /var/log/chworker-cloud-init.log 2>&1
    echo "chworker: start $(date -u +%FT%TZ)"
    systemctl enable --now docker
    curl -sL https://aka.ms/InstallAzureCLIDeb | bash
    az login --service-principal --username '{pe_client}' --password '{pe_secret}' --tenant '{pe_tenant}'
    az account set --subscription '{sub}'
    az acr login --name {acr}
    docker pull {image}
    mkdir -p /mnt/resource/pipeline-scratch && chmod 1777 /mnt/resource/pipeline-scratch
    set +e
    docker run --rm --network host \\
      -v /mnt/resource/pipeline-scratch:/scratch -e TMPDIR=/scratch \\
      -e CHATHEALTHY_NODE_IDENTITY='pipeline-worker' \\
      -e CH_SPACE_NAME='worker' -e CH_COMPONENT='worker' \\
      -e CH_LOG_DESTINATION='stderr,mongo' -e CH_LOG_DB='{_env("CH_LOG_DB")}' \\
      -e CH_MONGO_HOST_CHATHEALTHYFRONTEND='{_env("CH_MONGO_HOST_CHATHEALTHYFRONTEND")}' \\
      -e CH_MONGO_HOST_CHATHEALTHYDATAPIPELINES='{_env("CH_MONGO_HOST_CHATHEALTHYDATAPIPELINES")}' \\
      -e RUN_ID='{run_id}' -e ENV_PREFIX='{_env("ENV_PREFIX", "dev")}' \\
      -e KEY_VAULT_URI='{_env("KEY_VAULT_URI")}' \\
      -e PIPELINEEDITOR_AZURE_TENANT_ID='{pe_tenant}' \\
      -e PIPELINEEDITOR_AZURE_CLIENT_ID='{pe_client}' \\
      -e PIPELINEEDITOR_AZURE_CLIENT_SECRET='{pe_secret}' \\
      -e PIPELINE_SECRET_NAMES='{_env("PIPELINE_SECRET_NAMES")}' \\
      --entrypoint python {image} \\
      pipeline/run_lifecycle/bootstrap.py pipeline/run_lifecycle/pipeline_worker.py {step} --replica {replica} --run-id {run_id}
    echo "chworker: worker exit=$? $(date -u +%FT%TZ)"
    set -e
    az vm delete --resource-group {rg} --name {vm_name} --yes --no-wait || true
"""
    return base64.b64encode(yaml_body.encode("utf-8")).decode("ascii")


def _short(run_id: str) -> str:
    return run_id.split("-")[-1][:8] if "-" in run_id else run_id[:8]


def provision_worker_vm(run_id: str, step: str, replica: int) -> dict:
    """PUT one ephemeral Worker VM into the pipeline compute subnet, booting the
    worker cloud-init. Async: ARM returns a provisioning handle, not a finished
    VM. The Controller does not wait on it - it waits on the work_item the VM
    will claim, through the heartbeat loop it already runs."""
    token = _controller_arm_token()
    sub = _env("AZURE_SUBSCRIPTION_ID")
    rg = _env("AZURE_RESOURCE_GROUP")
    location = _env("AUTOMATION_VM_LOCATION", "eastus2")
    size = _env("WORKER_VM_SIZE", _env("AUTOMATION_VM_SIZE", "Standard_D32s_v6"))
    vnet = _env("AUTOMATION_VM_VNET", "vnet-chathealthy-pipeline-dev")
    subnet = _env("AUTOMATION_VM_SUBNET", "snet-pipeline-compute")
    if not (sub and rg):
        raise ChatHealthyException(
            mode="config_error",
            message="AZURE_SUBSCRIPTION_ID and AZURE_RESOURCE_GROUP are required "
                    "to provision a Worker VM",
            component="worker_vm_provisioning")
    vm_name = f"vm-chworker-{_short(run_id)}-{step}-{replica}"[:63]
    nic_name = f"{vm_name}-nic"
    tags = {"pipeline_run_id": run_id, "pipeline_step": step,
            "replica": str(replica), "env": _env("ENV_PREFIX", "dev")}
    subnet_id = (f"/subscriptions/{sub}/resourceGroups/{rg}"
                 f"/providers/Microsoft.Network/virtualNetworks/{vnet}"
                 f"/subnets/{subnet}")
    nic_url = (f"{_ARM}/subscriptions/{sub}/resourceGroups/{rg}"
               f"/providers/Microsoft.Network/networkInterfaces/{nic_name}"
               f"?api-version={_NIC_API}")
    _put(nic_url, {
        "location": location, "tags": tags,
        "properties": {"ipConfigurations": [{
            "name": "ipconfig1",
            "properties": {"subnet": {"id": subnet_id},
                           "privateIPAllocationMethod": "Dynamic"}}]}},
        token)
    user_data = _worker_cloud_init(run_id, step, replica)
    vm_url = (f"{_ARM}/subscriptions/{sub}/resourceGroups/{rg}"
              f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
              f"?api-version={_VM_API}")
    nic_id = (f"/subscriptions/{sub}/resourceGroups/{rg}"
              f"/providers/Microsoft.Network/networkInterfaces/{nic_name}")
    vm = _put(vm_url, {
        "location": location, "tags": tags,
        "properties": {
            "hardwareProfile": {"vmSize": size},
            "storageProfile": {
                "imageReference": {"publisher": "Canonical",
                                   "offer": "ubuntu-24_04-lts",
                                   "sku": "server", "version": "latest"},
                "osDisk": {"createOption": "FromImage",
                           "managedDisk": {"storageAccountType": "Premium_LRS"},
                           "deleteOption": "Delete"}},
            "osProfile": {
                "computerName": vm_name[:15],
                "adminUsername": "chpipeline",
                "linuxConfiguration": {
                    "disablePasswordAuthentication": True,
                    "ssh": {"publicKeys": [{
                        "path": "/home/chpipeline/.ssh/authorized_keys",
                        "keyData": _env("AZ_VM_ADMIN_SSH_PUBKEY")}]},
                    "provisionVMAgent": True},
                "customData": user_data},
            "networkProfile": {"networkInterfaces": [{
                "id": nic_id,
                "properties": {"primary": True, "deleteOption": "Delete"}}]},
            "userData": user_data}},
        token)
    _log.LogPipeline("INFO",
        "worker_vm provisioned run_id=%s step=%s replica=%d vm=%s",
        run_id, step, replica, vm_name)
    return {"vm_name": vm_name, "vm": vm}
