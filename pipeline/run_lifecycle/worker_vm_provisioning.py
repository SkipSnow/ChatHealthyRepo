"""worker_vm_provisioning.py - allocate the run's single Worker VM (two-VM shape).

The two-VM run (LLD Part I, I.1.1.3 / I.2.3.1-3) splits a run across a small
Controller VM and ONE big Worker VM. The Controller enqueues one work_item per
partition onto the always-on front-end cluster exactly as it does for the
single-VM shape, and - instead of spawning local worker subprocesses - allocates
the run's single Worker VM through allocate_worker_vm below. That box boots
worker_host.py, which drains every partition of every step as a bounded pool of
pipeline_worker processes, then self-deletes. Workers are PROCESSES on the one
box, not a VM per partition, so the hardware a run needs is this single box sized
once. The Controller never holds a handle on the Worker VM; it watches the
work_items, the same substrate it already uses for subprocess workers.

This path is reached only when a pipeline declares worker_compute='vm'; every
current pipeline (provider included) leaves it unset and keeps the subprocess
path untouched. The Controller authenticates to Azure ARM as pipelineController
(whose rights include creating the Worker VM); the Worker VM carries
pipelineEditor credentials for its own Mongo certificate auth, exactly as the
single-VM workers do today.

Every value this module needs is read from the Controller VM's environment and
is REQUIRED: there are no defaults and no infra identifiers in this source. The
Controller VM's environment is supplied by the trigger runbook's cloud-init,
which sources every value from the deploy (Automation Variables), so the facts
live in the deploy, never in code.
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


def _req(name: str) -> str:
    """A required environment value. Absent or empty is fatal: there is no
    default and no fallback. The fact must be supplied by the deploy."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ChatHealthyException(
            mode="config_error",
            message=f"worker_vm_provisioning: required value {name!r} is absent "
                    f"from the Controller environment; it must be supplied by the "
                    f"deploy. No default is applied.",
            component="worker_vm_provisioning", missing=name)
    return value


def _controller_arm_token() -> str:
    """An ARM token obtained as pipelineController, the identity the Controller
    runs under. Reads the pipelineController service-principal credentials from
    the Controller VM's environment; a missing credential aborts."""
    tenant = _req("PIPELINECONTROLLER_AZURE_TENANT_ID")
    client = _req("PIPELINECONTROLLER_AZURE_CLIENT_ID")
    secret = _req("PIPELINECONTROLLER_AZURE_CLIENT_SECRET")
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
            component="worker_vm_provisioning", exception=exc) from exc


def _short(run_id: str) -> str:
    return run_id.split("-")[-1][:8] if "-" in run_id else run_id[:8]


def _worker_host_cloud_init(run_id: str, pipeline_name: str, vm_name: str) -> str:
    """Base64 cloud-init for the run's single Worker VM: pull the pipeline
    image, run bootstrap.py + worker_host.py (bootstrap fetches the Mongo cert
    into env; the host then drains the run's work_items as a bounded pool of
    pipeline_worker processes across every step), then self-delete the VM when
    the host returns. pipelineEditor is the identity the box presents to Azure
    (ACR pull, farewell delete) and to Mongo (cert auth)."""
    acr = _req("AUTOMATION_VM_ACR")
    repo = _req("AUTOMATION_VM_IMAGE_REPO")
    tag = _req("AUTOMATION_VM_IMAGE_TAG")
    image = f"{acr}.azurecr.io/{repo}:{tag}"
    sub = _req("AZURE_SUBSCRIPTION_ID")
    rg = _req("AZURE_RESOURCE_GROUP")
    pe_tenant = _req("PIPELINEEDITOR_AZURE_TENANT_ID")
    pe_client = _req("PIPELINEEDITOR_AZURE_CLIENT_ID")
    pe_secret = _req("PIPELINEEDITOR_AZURE_CLIENT_SECRET")
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
      -e CH_LOG_DESTINATION='stderr,mongo' -e CH_LOG_DB='{_req("CH_LOG_DB")}' \\
      -e CH_MONGO_HOST_CHATHEALTHYFRONTEND='{_req("CH_MONGO_HOST_CHATHEALTHYFRONTEND")}' \\
      -e CH_MONGO_HOST_CHATHEALTHYDATAPIPELINES='{_req("CH_MONGO_HOST_CHATHEALTHYDATAPIPELINES")}' \\
      -e RUN_ID='{run_id}' -e ENV_PREFIX='{_req("ENV_PREFIX")}' \\
      -e PIPELINE_NAME='{pipeline_name}' \\
      -e PIPELINE_LOG_ACCOUNT_URL='{_req("PIPELINE_LOG_ACCOUNT_URL")}' \\
      -e AZURE_VM_NAME='{vm_name}' \\
      -e AZURE_SUBSCRIPTION_ID='{sub}' -e AZURE_RESOURCE_GROUP='{rg}' \\
      -e AZURE_TENANT_ID='{pe_tenant}' -e AZURE_CLIENT_ID='{pe_client}' -e AZURE_CLIENT_SECRET='{pe_secret}' \\
      -e KEY_VAULT_URI='{_req("KEY_VAULT_URI")}' \\
      -e PIPELINEEDITOR_AZURE_TENANT_ID='{pe_tenant}' \\
      -e PIPELINEEDITOR_AZURE_CLIENT_ID='{pe_client}' \\
      -e PIPELINEEDITOR_AZURE_CLIENT_SECRET='{pe_secret}' \\
      -e PIPELINE_SECRET_NAMES='{_req("PIPELINE_SECRET_NAMES")}' \\
      --entrypoint python {image} \\
      pipeline/run_lifecycle/bootstrap.py pipeline/run_lifecycle/worker_host.py
    echo "chworker: host exit=$? $(date -u +%FT%TZ)"
    set -e
    az vm delete --resource-group {rg} --name {vm_name} --yes --no-wait || true
"""
    return base64.b64encode(yaml_body.encode("utf-8")).decode("ascii")


def allocate_worker_vm(run_id: str, pipeline_name: str) -> dict:
    """The common hardware-allocation function: PUT the run's single ephemeral
    Worker VM into the pipeline compute subnet, booting the worker-host
    cloud-init. One box per run - the resident host drains every partition of
    every step as a bounded pool of processes, so the hardware a run needs is
    this one box sized once, not a VM per partition. Every pipeline calls this
    function. Async: ARM returns a provisioning handle, not a finished VM; the
    Controller waits on the work_items the host will drain, not on the VM. Every
    deploy fact is read required from the environment; nothing is defaulted in
    code."""
    token = _controller_arm_token()
    sub = _req("AZURE_SUBSCRIPTION_ID")
    rg = _req("AZURE_RESOURCE_GROUP")
    location = _req("AUTOMATION_VM_LOCATION")
    size = _req("WORKER_VM_SIZE")
    vnet = _req("AUTOMATION_VM_VNET")
    subnet = _req("AUTOMATION_VM_SUBNET")
    env_prefix = _req("ENV_PREFIX")
    ssh_key = _req("AZ_VM_ADMIN_SSH_PUBKEY")
    vm_name = f"vm-chworker-{_short(run_id)}"[:63]
    nic_name = f"{vm_name}-nic"
    tags = {"pipeline_run_id": run_id, "pipeline_name": pipeline_name,
            "env": env_prefix}
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
    user_data = _worker_host_cloud_init(run_id, pipeline_name, vm_name)
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
                        "keyData": ssh_key}]},
                    "provisionVMAgent": True},
                "customData": user_data},
            "networkProfile": {"networkInterfaces": [{
                "id": nic_id,
                "properties": {"primary": True, "deleteOption": "Delete"}}]},
            "userData": user_data}},
        token)
    _log.LogPipeline("INFO",
        "worker VM allocated run_id=%s pipeline=%s vm=%s",
        run_id, pipeline_name, vm_name)
    return {"vm_name": vm_name, "vm": vm}
