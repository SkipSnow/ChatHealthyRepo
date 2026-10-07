"""medicare_pipeline_runbook.py -- trigger tier for the Medicare two-VM run.

Runs in Azure Automation as a Python 3 runbook, fired by schedule or webhook.
It is the trigger tier for the Medicare Provider Evaluation pipeline
(EPIC-010-F-007), which runs the two-VM compute shape (see the
pipeline_run_compute_deployment diagram and the pipeline LLD "Two-VM Run Compute
Topology" section).

Duties, mirroring the provider runbook but creating a Controller VM rather than a
combined run VM:
  1. Read the webhook payload (data_version is mandatory; build_indication_map
     and states optional -- states defaults to ["ALL"] and is forwarded to the
     Controller as --states, driving the per-state fan-out exactly as provider).
  2. Acquire the per-pipeline lock and write a run manifest on the always-on
     front-end cluster.
  3. ARM-PUT one small Controller VM, authenticating as pipelineEditor (which
     holds Virtual Machine + Network Contributor, unchanged from provider). The
     Controller VM's cloud-init runs the Medicare control_runner with
     WORKER_COMPUTE=vm and the pipelineController credentials, so the Controller
     itself provisions the Worker VMs at fan-out.
  4. Write the run reservation and exit. The runbook does not wait for the run.

The Controller VM carries pipelineController's credentials (for creating Worker
VMs through ARM and for its own front-end Mongo cert auth) and pipelineEditor's
credentials (handed to each Worker VM for its Mongo + ARM, exactly as the
single-VM workers authenticate today). This runbook completes in seconds; all
long-running work happens on the Controller and Worker VMs.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import subprocess
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

_REQUIRED_PACKAGES = ["azure-identity", "azure-keyvault-secrets", "pymongo", "cryptography"]
for _pkg in _REQUIRED_PACKAGES:
    try:
        __import__(_pkg.replace("-", "_"))
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", _pkg])

# Point dnspython at public resolvers before pymongo binds the default resolver
# (the AA sandbox resolver does not answer external SRV queries).
try:
    import dns.resolver  # type: ignore[import-not-found]
    _r = dns.resolver.Resolver(configure=False)
    _r.nameservers = ["8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1"]
    _r.timeout = 5
    _r.lifetime = 10
    dns.resolver.default_resolver = _r
except ImportError:
    pass

from chathealthy_lib.logging_service import (  # noqa: E402
    ChatHealthyLoggingService, set_mongo_log_identity)
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

# Hydrate Automation Variables into os.environ before anything reads them.
for _k in ("CH_LOG_DB", "CH_LOG_LEVEL", "PIPELINE_SECRET_NAMES", "CH_EMBEDDING_MODEL",
           "KEY_VAULT_URI", "AUTOMATION_ENV_PREFIX",
           "AUTOMATION_SUBSCRIPTION_ID", "AUTOMATION_RESOURCE_GROUP",
           "ATLAS_PROJECT_ID", "AZ_VM_ADMIN_SSH_PUBKEY",
           "CONTROLLER_VM_SIZE", "WORKER_VM_SIZE",
           "AUTOMATION_VM_LOCATION", "AUTOMATION_VM_SUBNET", "AUTOMATION_VM_VNET",
           "AUTOMATION_VM_ACR", "AUTOMATION_VM_IMAGE_REPO", "AUTOMATION_VM_IMAGE_TAG",
           "CH_MONGO_HOST_CHATHEALTHYFRONTEND", "CH_MONGO_HOST_CHATHEALTHYDATAPIPELINES",
           "PIPELINE_LOG_ACCOUNT_URL",
           "PIPELINEEDITOR_AZURE_TENANT_ID", "PIPELINEEDITOR_AZURE_CLIENT_ID",
           "PIPELINEEDITOR_AZURE_CLIENT_SECRET",
           "PIPELINECONTROLLER_AZURE_TENANT_ID", "PIPELINECONTROLLER_AZURE_CLIENT_ID",
           "PIPELINECONTROLLER_AZURE_CLIENT_SECRET"):
    try:
        import automationassets  # only present in the AA sandbox
        _v = automationassets.get_automation_variable(_k)
        if _v:
            os.environ[_k] = str(_v)
    except Exception:  # noqa: BLE001
        pass

def _req(name: str) -> str:
    """A required value from the runbook's environment (an Automation Variable
    in AA). Absent or empty is fatal: no default, no fallback. The fact is
    supplied by the deploy, never hardcoded here."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ChatHealthyException(
            mode="config_error",
            message=f"medicare_pipeline_runbook: required value {name!r} is absent; "
                    f"it must be supplied by the deploy. No default is applied.",
            component="medicare_pipeline_runbook", missing=name)
    return value


set_mongo_log_identity("pipelineEditor")
os.environ.setdefault("CH_LOG_DESTINATION", "stderr,mongo")
os.environ.setdefault("CH_SPACE_NAME", "runbook")
os.environ["ENV_PREFIX"] = _req("AUTOMATION_ENV_PREFIX")
os.environ.setdefault("CH_COMPONENT", "medicare_pipeline_runbook")

PIPELINE_ADMIN_DB = "pipelineAdmin"
PIPELINE_NAME = "medicare"

# Every deploy fact below is supplied by the deploy (an Automation Variable);
# none defaults and none is hardcoded here -- the repository is public.
SUBSCRIPTION_ID = _req("AUTOMATION_SUBSCRIPTION_ID")
RESOURCE_GROUP = _req("AUTOMATION_RESOURCE_GROUP")
ENV_PREFIX = os.environ["ENV_PREFIX"]
CONTROLLER_VM_SIZE = _req("CONTROLLER_VM_SIZE")
WORKER_VM_SIZE = _req("WORKER_VM_SIZE")
VM_LOCATION = _req("AUTOMATION_VM_LOCATION")
VM_SUBNET = _req("AUTOMATION_VM_SUBNET")
VM_VNET = _req("AUTOMATION_VM_VNET")
VM_ACR = _req("AUTOMATION_VM_ACR")
VM_IMAGE_REPO = _req("AUTOMATION_VM_IMAGE_REPO")
VM_IMAGE_TAG = _req("AUTOMATION_VM_IMAGE_TAG")
KEY_VAULT_URI = _req("KEY_VAULT_URI")
PIPELINE_SECRET_NAMES = _req("PIPELINE_SECRET_NAMES")
CH_EMBEDDING_MODEL = _req("CH_EMBEDDING_MODEL")
CH_LOG_DB = _req("CH_LOG_DB")
PIPELINE_LOG_ACCOUNT_URL = _req("PIPELINE_LOG_ACCOUNT_URL")
MONGO_HOST_FRONTEND = _req("CH_MONGO_HOST_CHATHEALTHYFRONTEND")
MONGO_HOST_PIPELINE = _req("CH_MONGO_HOST_CHATHEALTHYDATAPIPELINES")
ATLAS_PIPELINE_CLUSTER = "ChatHealthyDataPipelines"
INVOCATION_MODE = "scheduled"
DEBUG_LEVEL_DEFAULT = "INFO"
_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_HOSTNAME = socket.gethostname()
RESERVATION_TTL_HOURS = 24
_PIPELINE_LOCK_TTL_HOURS = 12

_log = ChatHealthyLoggingService()


def _editor_credential() -> tuple[str, str, str]:
    keys = ("PIPELINEEDITOR_AZURE_TENANT_ID", "PIPELINEEDITOR_AZURE_CLIENT_ID",
            "PIPELINEEDITOR_AZURE_CLIENT_SECRET")
    vals = [os.environ.get(k, "").strip() for k in keys]
    missing = [k for k, v in zip(keys, vals) if not v]
    if missing:
        raise ChatHealthyException(
            mode="identity_credential_absent",
            message="cannot act as pipelineEditor: " + ", ".join(missing) + " absent",
            component="medicare_pipeline_runbook", missing=",".join(missing))
    return vals[0], vals[1], vals[2]


def _controller_credential() -> tuple[str, str, str]:
    keys = ("PIPELINECONTROLLER_AZURE_TENANT_ID", "PIPELINECONTROLLER_AZURE_CLIENT_ID",
            "PIPELINECONTROLLER_AZURE_CLIENT_SECRET")
    vals = [os.environ.get(k, "").strip() for k in keys]
    missing = [k for k, v in zip(keys, vals) if not v]
    if missing:
        raise ChatHealthyException(
            mode="identity_credential_absent",
            message="cannot hand the Controller VM pipelineController credentials: "
                    + ", ".join(missing) + " absent",
            component="medicare_pipeline_runbook", missing=",".join(missing))
    return vals[0], vals[1], vals[2]


EDITOR_TENANT, EDITOR_CLIENT, EDITOR_SECRET = _editor_credential()
_TOKEN_CACHE: dict = {}


def _get_token(resource: str) -> str:
    if resource in _TOKEN_CACHE:
        return _TOKEN_CACHE[resource]
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": EDITOR_CLIENT,
        "client_secret": EDITOR_SECRET,
        "scope": resource.rstrip("/") + "/.default",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{EDITOR_TENANT}/oauth2/v2.0/token",
        data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        _TOKEN_CACHE[resource] = json.loads(r.read().decode("utf-8"))["access_token"]
    return _TOKEN_CACHE[resource]


def log(event: str, **fields):
    __ch_log_wrapper__ = True  # noqa: F841
    record = {"ts": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
              "runbook": PIPELINE_NAME + "_pipeline_runbook",
              "hostname": _HOSTNAME, "event": event}
    record.update(fields)
    _log.LogPipeline("INFO", json.dumps(record, default=str))


def _short(run_id: str) -> str:
    return run_id.split("-")[-1][:8] if "-" in run_id else run_id[:8]


def _pipeline_lock_id(name: str) -> str:
    return f"pipeline_lock:{name}"


def _acquire_pipeline_lock(mongo, run_id: str, vm_name: str) -> dict | None:
    from datetime import datetime, timedelta, timezone
    from pymongo.errors import DuplicateKeyError
    coll = mongo[PIPELINE_ADMIN_DB]["cluster_lifecycle"]
    now = datetime.now(timezone.utc)
    doc = {"_id": _pipeline_lock_id(PIPELINE_NAME), "kind": "pipeline_lock",
           "pipeline_name": PIPELINE_NAME, "run_id": run_id, "vm_name": vm_name,
           "acquired_at": now.isoformat(),
           "expires_at": (now + timedelta(hours=_PIPELINE_LOCK_TTL_HOURS)).isoformat()}
    try:
        coll.insert_one(doc)
        return None
    except DuplicateKeyError:
        existing = coll.find_one({"_id": doc["_id"]})
        if existing:
            exp = existing.get("expires_at") or ""
            if exp and exp < now.isoformat():
                coll.delete_one({"_id": doc["_id"], "run_id": existing.get("run_id")})
                try:
                    coll.insert_one(doc)
                    return None
                except DuplicateKeyError:
                    return coll.find_one({"_id": doc["_id"]})
        return existing


def _release_pipeline_lock(mongo, run_id: str) -> None:
    mongo[PIPELINE_ADMIN_DB]["cluster_lifecycle"].delete_one(
        {"_id": _pipeline_lock_id(PIPELINE_NAME), "run_id": run_id})


def _write_run_manifest(mongo, run_id: str, data_version: int,
                        build_indication_map: bool, invocation_mode: str,
                        states: list[str]) -> None:
    mongo[PIPELINE_ADMIN_DB]["pipeline.runs"].insert_one({
        "run_id": run_id, "pipeline_name": PIPELINE_NAME, "env": ENV_PREFIX,
        "status": "running", "started_at": datetime.datetime.utcnow(),
        "invocation_mode": invocation_mode, "data_version": data_version,
        "build_indication_map": build_indication_map, "states": states})


def _create_reservation(mongo, run_id: str, vm_name: str) -> None:
    now = datetime.datetime.utcnow()
    mongo[PIPELINE_ADMIN_DB]["cluster_lifecycle"].replace_one(
        {"_id": run_id},
        {"_id": run_id, "run_id": run_id, "vm_name": vm_name,
         "cluster_name": ATLAS_PIPELINE_CLUSTER, "requester": "medicare_pipeline_runbook",
         "start_time": now, "expiry_at": now + datetime.timedelta(hours=RESERVATION_TTL_HOURS),
         "reservation_class": "pipeline_run", "pipeline_name": PIPELINE_NAME,
         "status": "active"},
        upsert=True)


def _cancel_reservation(mongo, run_id: str) -> None:
    mongo[PIPELINE_ADMIN_DB]["cluster_lifecycle"].delete_one({"_id": run_id})


def _get_ssh_pubkey() -> str:
    return _req("AZ_VM_ADMIN_SSH_PUBKEY")


def _controller_cloud_init(run_id: str, data_version: int,
                           build_indication_map: bool, vm_name: str,
                           debug_level: str, states: list[str]) -> str:
    """Base64 cloud-init for the Controller VM: pull the image, run the Medicare
    control_runner with WORKER_COMPUTE=vm so the orchestrator provisions Worker
    VMs, then self-delete. The Controller presents pipelineController for ARM
    (Worker-VM creation) and its own Mongo cert auth; pipelineEditor credentials
    are handed down for the Worker VMs."""
    import base64
    ct, cc, cs = _controller_credential()
    image = f"{VM_ACR}.azurecr.io/{VM_IMAGE_REPO}:{VM_IMAGE_TAG}"
    bim = "--build-indication-map" if build_indication_map else ""
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
    exec >> /var/log/chcontrol-cloud-init.log 2>&1
    echo "chcontrol: start $(date -u +%FT%TZ)"
    systemctl enable --now docker
    curl -sL https://aka.ms/InstallAzureCLIDeb | bash
    az login --service-principal --username '{EDITOR_CLIENT}' --password '{EDITOR_SECRET}' --tenant '{EDITOR_TENANT}'
    az account set --subscription '{SUBSCRIPTION_ID}'
    az acr login --name {VM_ACR}
    docker pull {image}
    mkdir -p /mnt/resource/pipeline-scratch && chmod 1777 /mnt/resource/pipeline-scratch
    set +e
    docker run --rm --network host \\
      -v /mnt/resource/pipeline-scratch:/scratch -e TMPDIR=/scratch \\
      -e CHATHEALTHY_NODE_IDENTITY='pipeline-control' \\
      -e CH_SPACE_NAME='control' -e CH_COMPONENT='medicare_pipeline_control' \\
      -e CH_LOG_DESTINATION='stderr,mongo' -e CH_LOG_LEVEL='{debug_level}' -e CH_LOG_DB='{CH_LOG_DB}' \\
      -e CH_MONGO_HOST_CHATHEALTHYFRONTEND='{MONGO_HOST_FRONTEND}' \\
      -e CH_MONGO_HOST_CHATHEALTHYDATAPIPELINES='{MONGO_HOST_PIPELINE}' \\
      -e RUN_ID='{run_id}' -e DATA_VERSION='{data_version}' -e ENV_PREFIX='{ENV_PREFIX}' \\
      -e PIPELINE_NAME='medicare' -e GOOGLE_MAPS_ENABLED='0' \\
      -e PIPELINE_LOG_ACCOUNT_URL='{PIPELINE_LOG_ACCOUNT_URL}' \\
      -e AZURE_VM_NAME='{vm_name}' \\
      -e AZURE_TENANT_ID='{EDITOR_TENANT}' -e AZURE_CLIENT_ID='{EDITOR_CLIENT}' -e AZURE_CLIENT_SECRET='{EDITOR_SECRET}' \\
      -e WORKER_COMPUTE='vm' \\
      -e AZURE_SUBSCRIPTION_ID='{SUBSCRIPTION_ID}' -e AZURE_RESOURCE_GROUP='{RESOURCE_GROUP}' \\
      -e AUTOMATION_VM_LOCATION='{VM_LOCATION}' -e AUTOMATION_VM_VNET='{VM_VNET}' \\
      -e AUTOMATION_VM_SUBNET='{VM_SUBNET}' -e AUTOMATION_VM_ACR='{VM_ACR}' \\
      -e AUTOMATION_VM_IMAGE_REPO='{VM_IMAGE_REPO}' -e AUTOMATION_VM_IMAGE_TAG='{VM_IMAGE_TAG}' \\
      -e WORKER_VM_SIZE='{WORKER_VM_SIZE}' \\
      -e AZ_VM_ADMIN_SSH_PUBKEY='{_get_ssh_pubkey()}' \\
      -e KEY_VAULT_URI='{KEY_VAULT_URI}' -e PIPELINE_SECRET_NAMES='{PIPELINE_SECRET_NAMES}' \\
      -e CH_EMBEDDING_MODEL='{CH_EMBEDDING_MODEL}' \\
      -e PIPELINECONTROLLER_AZURE_TENANT_ID='{ct}' \\
      -e PIPELINECONTROLLER_AZURE_CLIENT_ID='{cc}' \\
      -e PIPELINECONTROLLER_AZURE_CLIENT_SECRET='{cs}' \\
      -e PIPELINEEDITOR_AZURE_TENANT_ID='{EDITOR_TENANT}' \\
      -e PIPELINEEDITOR_AZURE_CLIENT_ID='{EDITOR_CLIENT}' \\
      -e PIPELINEEDITOR_AZURE_CLIENT_SECRET='{EDITOR_SECRET}' \\
      --entrypoint python {image} \\
      pipeline/run_lifecycle/bootstrap.py \\
      pipeline/MedicareProviderEvaluationDataPipeline/control_runner.py \\
      --run-id {run_id} --env-prefix {ENV_PREFIX} --data-version {data_version} --states {",".join(states)} {bim}
    echo "chcontrol: controller exit=$? $(date -u +%FT%TZ)"
    set -e
    az login --service-principal --username '{cc}' --password '{cs}' --tenant '{ct}' || true
    az vm delete --resource-group {RESOURCE_GROUP} --name {vm_name} --yes --no-wait || true
"""
    return base64.b64encode(yaml_body.encode("utf-8")).decode("ascii")


def _put(url: str, body: dict, tok: str) -> dict:
    req = urllib.request.Request(
        url, method="PUT",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        data=json.dumps(body).encode("utf-8"))
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
            component="medicare_pipeline_runbook", exception=exc) from exc


def _provision_controller_vm(run_id: str, data_version: int,
                             build_indication_map: bool, debug_level: str,
                             states: list[str]) -> dict:
    tok = _get_token("https://management.azure.com/")
    vm_name = f"vm-chcontrol-{_short(run_id)}"
    nic_name = f"{vm_name}-nic"
    tags = {"pipeline_run_id": run_id, "pipeline_name": PIPELINE_NAME, "env": ENV_PREFIX}
    subnet_id = (f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
                 f"/providers/Microsoft.Network/virtualNetworks/{VM_VNET}/subnets/{VM_SUBNET}")
    nic_url = (f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
               f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/"
               f"networkInterfaces/{nic_name}?api-version=2023-09-01")
    _put(nic_url, {"location": VM_LOCATION, "tags": tags,
                   "properties": {"ipConfigurations": [{"name": "ipconfig1",
                       "properties": {"subnet": {"id": subnet_id},
                                      "privateIPAllocationMethod": "Dynamic"}}]}}, tok)
    user_data = _controller_cloud_init(run_id, data_version, build_indication_map,
                                       vm_name, debug_level, states)
    vm_url = (f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
              f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Compute/"
              f"virtualMachines/{vm_name}?api-version=2024-03-01")
    nic_id = (f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
              f"/providers/Microsoft.Network/networkInterfaces/{nic_name}")
    _put(vm_url, {"location": VM_LOCATION, "tags": tags, "properties": {
        "hardwareProfile": {"vmSize": CONTROLLER_VM_SIZE},
        "storageProfile": {"imageReference": {"publisher": "Canonical",
                                              "offer": "ubuntu-24_04-lts",
                                              "sku": "server", "version": "latest"},
                           "osDisk": {"createOption": "FromImage",
                                      "managedDisk": {"storageAccountType": "Premium_LRS"},
                                      "deleteOption": "Delete"}},
        "osProfile": {"computerName": vm_name[:15], "adminUsername": "chpipeline",
                      "linuxConfiguration": {"disablePasswordAuthentication": True,
                          "ssh": {"publicKeys": [{"path": "/home/chpipeline/.ssh/authorized_keys",
                                                  "keyData": _get_ssh_pubkey()}]},
                          "provisionVMAgent": True},
                      "customData": user_data},
        "networkProfile": {"networkInterfaces": [{"id": nic_id,
            "properties": {"primary": True, "deleteOption": "Delete"}}]},
        "userData": user_data}}, tok)
    return {"vm_name": vm_name}


def _parse_webhook_input() -> dict:
    if len(sys.argv) > 1:
        raw = " ".join(str(a) for a in sys.argv[1:])
    else:
        raw = os.environ.get("WEBHOOKDATA", "")
    if not raw:
        return {}
    marker = "RequestBody:"
    idx = raw.find(marker)
    if idx < 0:
        try:
            return json.loads(raw) or {}
        except Exception:  # noqa: BLE001
            return {}
    start = idx + len(marker)
    while start < len(raw) and raw[start] != "{":
        start += 1
    depth, end = 0, start
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    try:
        parsed = json.loads(raw[start:end + 1])
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _raise_missing_data_version(body) -> None:
    raise ChatHealthyException(
        mode="value_error",
        message="medicare_pipeline_runbook: webhook payload MUST include data_version "
                "as int >= 1. Fire again with a valid value.",
        component="medicare_pipeline_runbook", webhook_body_head=str(body)[:400])


def _raise_illegal_states(raw) -> None:
    raise ChatHealthyException(
        mode="value_error",
        message="medicare_pipeline_runbook: states must be either [\"ALL\"] alone "
                "or a non-empty list of state codes with no \"ALL\" among them. "
                f"Got {raw!r}. Fire again with a legal scope.",
        component="medicare_pipeline_runbook")


def _resolve_states(raw) -> list[str]:
    """Parse the webhook states into the provider-identical scope: ["ALL"] alone
    (the whole country -- one worker per state plus the ALL catch-all), or a
    non-empty list of state codes with no "ALL" among them. Absent -> ["ALL"].
    Illegal shapes (empty, or "ALL" mixed with states) abend here, at the parse,
    so no downstream step has to guard them."""
    if raw is None:
        return ["ALL"]
    if isinstance(raw, list):
        scope = raw
    elif isinstance(raw, str):
        scope = None
        if raw.strip().startswith("["):
            try:
                parsed = json.loads(raw)
                scope = parsed if isinstance(parsed, list) else None
            except Exception:  # noqa: BLE001
                scope = None
        if scope is None:
            scope = raw.split(",")
    else:
        scope = ["ALL"]
    scope = [str(s).strip().upper() for s in scope if str(s).strip()]
    if not scope or ("ALL" in scope and len(scope) != 1):
        _raise_illegal_states(raw)
    return scope


def _run() -> int:
    run_id = str(uuid.uuid4())
    os.environ["RUN_ID"] = run_id
    body = _parse_webhook_input()
    invocation_mode = body.get("invocation_mode", "webhook") if body else INVOCATION_MODE
    try:
        data_version = int((body or {}).get("data_version"))
    except (TypeError, ValueError):
        data_version = 0
    if data_version < 1:
        log("runbook_reject_no_data_version", body=str(body)[:400])
        _raise_missing_data_version(body)
    bim_raw = (body or {}).get("build_indication_map")
    build_indication_map = (bim_raw is True
                            or str(bim_raw or "").strip().lower() in ("1", "true", "yes"))
    debug_level = str((body or {}).get("debug_level")
                      or DEBUG_LEVEL_DEFAULT).strip().upper()
    if debug_level not in _VALID_LOG_LEVELS:
        debug_level = DEBUG_LEVEL_DEFAULT
    # Fan-out scope. Accept `states` (and provider's `state_scope` alias);
    # absent -> ["ALL"] (the whole country). Provider-identical semantics.
    states_raw = (body or {}).get("states")
    if states_raw is None:
        states_raw = (body or {}).get("state_scope")
    states = _resolve_states(states_raw)

    log("runbook_start", pipeline=PIPELINE_NAME, env=ENV_PREFIX,
        invocation_mode=invocation_mode, data_version=data_version,
        build_indication_map=build_indication_map, debug_level=debug_level,
        states=states)

    runbook_owns_lock = False
    reservation_created = False
    vm_name = f"vm-chcontrol-{_short(run_id)}"
    mongo = None
    try:
        mongo = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")
        mongo.admin.command("ping")
        blocking = _acquire_pipeline_lock(mongo, run_id, vm_name)
        if blocking is not None:
            log("pipeline_already_running_abend", attempted_run_id=run_id,
                live_run_id=blocking.get("run_id"))
            return 1
        runbook_owns_lock = True
        _write_run_manifest(mongo, run_id, data_version, build_indication_map,
                            invocation_mode, states)
        log("run_manifest_written", run_id=run_id)

        _provision_controller_vm(run_id, data_version, build_indication_map,
                                 debug_level, states)
        runbook_owns_lock = False  # Controller owns lock release from here
        log("controller_vm_provisioned", run_id=run_id, vm_name=vm_name)
        _create_reservation(mongo, run_id, vm_name)
        reservation_created = True
        log("reservation_created", run_id=run_id, vm_name=vm_name)
        log("runbook_exit", run_id=run_id)
        return 0
    except Exception as exc:  # noqa: BLE001
        log("runbook_failed", run_id=run_id, error_type=type(exc).__name__,
            error=str(exc)[:1200], traceback=traceback.format_exc()[-1500:])
        if mongo is not None:
            if reservation_created:
                try:
                    _cancel_reservation(mongo, run_id)
                except Exception:  # noqa: BLE001
                    pass
            if runbook_owns_lock:
                try:
                    _release_pipeline_lock(mongo, run_id)
                except Exception:  # noqa: BLE001
                    pass
        return 1


def main() -> int:
    return _run()


if __name__ == "__main__":
    sys.exit(main())
