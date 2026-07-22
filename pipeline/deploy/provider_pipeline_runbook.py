"""provider_pipeline_runbook.py -- trigger tier per LLD v22 §2.1.

Runs in Azure Automation as a Python 3 runbook. Fires from either:
  (a) a Schedule attached to the runbook
  (b) a webhook exposed by the Automation Account

Duties (LLD §2.6 steps 1-4):
  1. Read pipeline.config from chathealthyfrontend for pipeline_name='provider'
  2. Write a fresh run manifest to chathealthyfrontend.pipeline.runs with
     status='running', started_at=now, invocation_mode
  3. Call ARM API to start the prov-control ACA Job with env-var overrides:
     RUN_ID, ENV_PREFIX, INVOCATION_MODE, LOAD_MODE, STATE_SCOPE
  4. Exit. Do not wait for the pipeline to complete.

Every event is logged to the pipeline-logs blob container in our datalake
(one append blob per pipeline per day). Instrumentation is the runbook's
sole visibility surface -- operators reading the datalake blob see
exactly what the trigger tier did on each fire.

Design constraint: this runbook completes in seconds. All long-running
work happens inside Control + Workers. This dodges the 3-hour Azure
Automation fair-share cap.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import sys
import traceback
import urllib.error
import urllib.request
import uuid


# ---------- Deploy-time invariants (Automation Variables in prod) ----------
SUBSCRIPTION_ID = os.environ.get(
    "AUTOMATION_SUBSCRIPTION_ID", "7a17eec1-c477-4c7c-b1c1-d0662ce7a1ee"
)
RESOURCE_GROUP = os.environ.get(
    "AUTOMATION_RESOURCE_GROUP", "rg-chathealthy-pipeline-dev"
)
ENV_PREFIX = os.environ.get("AUTOMATION_ENV_PREFIX", "dev")
PIPELINE_NAME = "provider"

# v32: Runbook creates a fresh VM per run (no ACA job).
VM_LOCATION = os.environ.get("AUTOMATION_VM_LOCATION", "eastus2")
VM_SIZE = os.environ.get("AUTOMATION_VM_SIZE", "Standard_D8s_v6")
VM_SUBNET = os.environ.get(
    "AUTOMATION_VM_SUBNET",
    "snet-pipeline-compute",
)
VM_VNET = os.environ.get(
    "AUTOMATION_VM_VNET",
    "vnet-chathealthy-pipeline-dev",
)
VM_MI_NAME = os.environ.get(
    "AUTOMATION_VM_MI_NAME",
    "mi-control",   # user-assigned MI attached to the Pipeline Run VM
)
VM_ACR = os.environ.get(
    "AUTOMATION_VM_ACR",
    "chpipelinedevacr",
)
VM_IMAGE_REPO = os.environ.get(
    "AUTOMATION_VM_IMAGE_REPO",
    "pipeline-control",
)
VM_IMAGE_TAG = os.environ.get(
    "AUTOMATION_VM_IMAGE_TAG",
    "latest",
)

# Atlas Admin API -- for wake-Mongo-in-parallel-with-vm-create.
ATLAS_ADMIN_BASE = os.environ.get(
    "AUTOMATION_ATLAS_ADMIN_BASE",
    "https://cloud.mongodb.com/api/atlas/v2",
)
ATLAS_PROJECT_ID = os.environ.get("AUTOMATION_ATLAS_PROJECT_ID", "")
ATLAS_PIPELINE_CLUSTER = os.environ.get(
    "AUTOMATION_ATLAS_PIPELINE_CLUSTER",
    "chathealthydatapipeline",
)

# Key Vault
KEY_VAULT_URI = os.environ.get(
    "KEY_VAULT_URI", "https://kv-chpipeline-dev.vault.azure.net/"
)

# Webhook / cron defaults
INVOCATION_MODE = os.environ.get("INVOCATION_MODE", "scheduled")
LOAD_MODE_DEFAULT = os.environ.get("LOAD_MODE", "full")
STATE_SCOPE_DEFAULT = os.environ.get("STATE_SCOPE", "ALL")

_HOSTNAME = socket.gethostname()


# ============================================================================
# Managed-identity token acquisition
# ============================================================================
def _load_user_mi_client_id() -> str:
    """Fetch the user-assigned MI's client_id from Automation Variable
    AZURE_CLIENT_ID (populated by the deploy chain during AA identity
    attach). Falls back to os.environ for non-Automation contexts.
    Empty string if neither is available (system-MI attempt will then
    fail with a clear Azure error)."""
    try:
        import automationassets  # only present in Automation sandbox
        val = automationassets.get_automation_variable("AZURE_CLIENT_ID")
        if val:
            return str(val).strip()
    except Exception:
        pass
    return os.environ.get("AZURE_CLIENT_ID", "").strip()


AZURE_CLIENT_ID = _load_user_mi_client_id()


def _get_token(resource: str, client_id: str | None = None) -> str:
    """Fetch an OAuth2 token from IMDS for the given resource. Works in
    both Azure Automation Runbook sandbox and any container/VM with
    IMDS available.

    F-003: the AA carries only a user-assigned managed identity
    (mi-runbook). IMDS returns a system-MI token by default and Azure
    responds `Managed System Identity not found!` because no system-MI
    is attached. The user-assigned MI's client_id MUST be passed on
    the token query to select it. AZURE_CLIENT_ID is set as an
    Automation Variable by the deploy chain during AA identity attach."""
    cid = client_id or AZURE_CLIENT_ID
    identity_endpoint = os.environ.get("IDENTITY_ENDPOINT")
    identity_header = os.environ.get("IDENTITY_HEADER")
    if identity_endpoint and identity_header:
        q = f"?resource={resource}&api-version=2019-08-01"
        if cid:
            q += f"&client_id={cid}"
        url = f"{identity_endpoint}{q}"
        req = urllib.request.Request(url, headers={"X-IDENTITY-HEADER": identity_header})
    else:
        q = f"?api-version=2018-02-01&resource={resource}"
        if cid:
            q += f"&client_id={cid}"
        url = f"http://169.254.169.254/metadata/identity/oauth2/token{q}"
        req = urllib.request.Request(url, headers={"Metadata": "true"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))["access_token"]


# ============================================================================
# Canonical Mongo logger -- writes every structured event as one document to
# chathealthyfrontend.logFileCollection per ReadMePipelineLogs.txt. Blob is
# NOT the runtime observability surface. On write failure the stage abends
# (silent operation with a broken observability surface is a fatal error).
# ============================================================================
_LOG_MONGO_HANDLE = None
_LOG_BUFFER = []


def _activate_mongo_logging(mongo):
    """Called by main() as soon as the front-cluster MongoClient is ready.
    Sets the module-level handle and drains any buffered events into
    logFileCollection. Abends on write failure per policy."""
    global _LOG_MONGO_HANDLE
    _LOG_MONGO_HANDLE = mongo["chathealthyfrontend"]["logFileCollection"]
    if _LOG_BUFFER:
        _LOG_MONGO_HANDLE.insert_many(_LOG_BUFFER)
        _LOG_BUFFER.clear()


def log(event: str, **fields):
    """One structured event -> chathealthyfrontend.logFileCollection.
    Also printed to stdout so it surfaces in the AA job streams for
    real-time debugging visibility (stdout mirror, not a destination).
    Events fired before the Mongo client opens buffer in memory; the
    buffer flushes when _activate_mongo_logging() runs. If the Mongo
    write fails the exception propagates -- per ReadMePipelineLogs.txt
    the stage MUST abend if the log surface is broken."""
    now = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    record = {
        "ts": now,
        "runbook": PIPELINE_NAME + "_pipeline_runbook",
        "hostname": _HOSTNAME,
        "event": event,
    }
    record.update(fields)
    print(json.dumps(record, default=str), flush=True)
    if _LOG_MONGO_HANDLE is None:
        _LOG_BUFFER.append(record)
        return
    _LOG_MONGO_HANDLE.insert_one(record)


# ============================================================================
# Mongo config + run manifest -- reads Mongo conn string from Key Vault
# ============================================================================
def _get_mongo_conn_string() -> str:
    """Fetch front-cluster connection string from Key Vault. Trigger-tier
    coordination (serialization guard, pipeline.runs manifest, pipeline.config
    read) lives on the ALWAYS-UP front cluster, not the pipeline cluster
    which is paused-by-default between runs (Skip 2026-07-18)."""
    name = os.environ.get("MONGO_SECRET_NAME", "MONGO-FRONTEND-connectionString")
    tok = _get_token("https://vault.azure.net")
    url = f"{KEY_VAULT_URI.rstrip('/')}/secrets/{name}?api-version=7.4"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))["value"]


def _doh_ssl_context():
    """SSL context for DoH -- uses certifi CA bundle if importable so the
    Automation sandbox's stripped-down trust store doesn't reject
    Cloudflare's cert chain."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _doh_query(name: str, rtype: str) -> list:
    """Query Cloudflare DNS-over-HTTPS for a single record type. Returns
    the Answer list (each item is a dict with `data` and `type`). Used
    to sidestep the Automation sandbox's broken outbound DNS resolver
    for Atlas SRV/TXT lookups."""
    url = f"https://cloudflare-dns.com/dns-query?name={name}&type={rtype}"
    req = urllib.request.Request(
        url, headers={"Accept": "application/dns-json"},
    )
    with urllib.request.urlopen(req, timeout=15, context=_doh_ssl_context()) as r:
        return json.loads(r.read().decode("utf-8")).get("Answer", []) or []


def _srv_to_direct_mongo_uri(srv_uri: str) -> str:
    """Translate a mongodb+srv:// URI to its non-SRV mongodb:// equivalent
    by resolving _mongodb._tcp.<host> SRV + <host> TXT via DNS-over-HTTPS,
    then reassembling the URI with the resolved hostnames and TXT options
    inline. Non-SRV inputs are returned unchanged."""
    if not srv_uri.startswith("mongodb+srv://"):
        return srv_uri
    tail = srv_uri[len("mongodb+srv://"):]
    if "@" in tail:
        userinfo, rest = tail.split("@", 1)
        userinfo = userinfo + "@"
    else:
        userinfo, rest = "", tail
    host_and_query = rest.split("/", 1)
    host_only = host_and_query[0]
    trailing = "/" + host_and_query[1] if len(host_and_query) > 1 else ""
    # SRV lookup for hosts:port
    srv_answers = _doh_query(f"_mongodb._tcp.{host_only}", "SRV")
    hosts = []
    for a in srv_answers:
        if a.get("type") != 33:  # SRV
            continue
        parts = a.get("data", "").strip().split()
        if len(parts) >= 4:
            port = parts[2]
            target = parts[3].rstrip(".")
            hosts.append(f"{target}:{port}")
    if not hosts:
        raise RuntimeError(f"DoH SRV lookup returned no hosts for {host_only}")
    # TXT lookup for connection options (Atlas ships tls/replicaSet/authSource)
    txt_answers = _doh_query(host_only, "TXT")
    txt_opts = ""
    for a in txt_answers:
        if a.get("type") != 16:  # TXT
            continue
        val = a.get("data", "").strip().strip('"')
        if val:
            txt_opts = val
            break
    # Assemble direct URI
    base = "mongodb://" + userinfo + ",".join(hosts) + trailing
    if txt_opts:
        sep = "&" if ("?" in base) else "?"
        base = base + sep + txt_opts
    # Atlas SRV implies tls=true; enforce.
    if "tls=" not in base and "ssl=" not in base:
        sep = "&" if ("?" in base) else "?"
        base = base + sep + "tls=true"
    return base


def _read_pipeline_config(mongo) -> dict:
    """LLD §2.6 step 2: read chathealthyfrontend.pipeline.config for
    pipeline_name='provider'. Returns config dict or empty dict if the
    document doesn't exist yet (first run)."""
    coll = mongo["chathealthyfrontend"]["pipeline.config"]
    doc = coll.find_one({"pipeline_name": PIPELINE_NAME, "env": ENV_PREFIX}) or {}
    return doc


def _find_live_pipeline_run(mongo) -> dict | None:
    """Runbook gatekeeper: return the first ACTIVE cluster_lifecycle
    reservation on the pipeline cluster, or None. Reservations are
    the canonical liveness signal -- Control/Worker acquire on start
    and release on finish, reservation_reaper clears stale entries.
    An active reservation means a real job is in flight (or was very
    recently); the runbook MUST abend without touching any shared
    resource."""
    pipeline_cluster = os.environ.get(
        "PIPELINE_CLUSTER_NAME", "ChatHealthyDataPipelines"
    )
    coll = mongo["admin"]["cluster_lifecycle"]
    return coll.find_one({
        "cluster_name": pipeline_cluster,
        "status": "active",
    })


def _write_run_manifest(mongo, run_id: str, load_mode: str,
                        state_scope, invocation_mode: str,
                        config: dict) -> None:
    """LLD §2.6 step 3: fresh manifest in chathealthyfrontend.pipeline.runs."""
    coll = mongo["chathealthyfrontend"]["pipeline.runs"]
    manifest = {
        "run_id": run_id,
        "pipeline_name": PIPELINE_NAME,
        "env": ENV_PREFIX,
        "status": "running",
        "started_at": datetime.datetime.utcnow(),
        "invocation_mode": invocation_mode,
        "load_mode": load_mode,
        "state_scope": state_scope,
        "config_snapshot": config,
    }
    coll.insert_one(manifest)


# ============================================================================
# ARM -- start the prov-control ACA Job with env-var overrides
# ============================================================================
def _cloud_init_user_data(run_id: str, load_mode: str, state_scope,
                          invocation_mode: str, resume_from_step: str) -> str:
    """Return the base64-encoded cloud-init user_data blob for the VM.

    The VM boots this cloud-init:
      1. Log in to ACR via the attached user-assigned MI
      2. Pull the pipeline image
      3. Run Controller with per-run env vars
      4. On Controller exit, the container ends; the finally-block in
         Controller fires `az vm delete` on this VM.
    """
    import base64
    # Note: `state_scope` may be a list; serialize to JSON so Controller
    # can parse it identically to how the ACA env var carried it.
    scope_json = json.dumps(state_scope)
    image_ref = f"{VM_ACR}.azurecr.io/{VM_IMAGE_REPO}:{VM_IMAGE_TAG}"
    short_id = run_id.split("-")[-1][:8] if "-" in run_id else run_id[:8]
    vm_name_for_farewell = f"vm-chpipeline-{short_id}"
    yaml_body = f"""#cloud-config
package_update: true
package_upgrade: false
packages:
  - ca-certificates
  - curl
  - gnupg
  - jq
  - docker.io
runcmd:
  - |
    set -eux
    exec > >(tee -a /var/log/chpipeline-cloud-init.log) 2>&1
    echo "chpipeline: cloud-init runcmd start $(date -u +%FT%TZ)"
    # docker.io installed via packages: above; enable + start.
    systemctl enable --now docker
    # Install Azure CLI via the Microsoft install script (Ubuntu 24.04 stock
    # has no az CLI; MI-based ACR login below needs it).
    curl -sL https://aka.ms/InstallAzureCLIDeb | bash
    # Login to ACR via the attached user-assigned MI (mi-control).
    az login --identity --allow-no-subscriptions
    az acr login --name {VM_ACR}
    # Pull the pipeline image.
    docker pull {image_ref}
    # Run Controller. On exit, container is gone; Controller's finally block
    # fires `az vm delete` on AZURE_VM_NAME before returning.
    docker run --rm --network host \\
      -e RUN_ID='{run_id}' \\
      -e ENV_PREFIX='{ENV_PREFIX}' \\
      -e INVOCATION_MODE='{invocation_mode}' \\
      -e LOAD_MODE='{load_mode}' \\
      -e STATE_SCOPE='{scope_json}' \\
      -e PIPELINE_NAME='{PIPELINE_NAME}' \\
      -e RESUME_FROM_STEP='{resume_from_step}' \\
      -e KEY_VAULT_URI='{KEY_VAULT_URI}' \\
      -e AZURE_RESOURCE_GROUP='{RESOURCE_GROUP}' \\
      -e AZURE_SUBSCRIPTION_ID='{SUBSCRIPTION_ID}' \\
      -e AZURE_VM_NAME='{vm_name_for_farewell}' \\
      {image_ref}
"""
    return base64.b64encode(yaml_body.encode("utf-8")).decode("ascii")


def _get_ssh_pubkey() -> str:
    """Fetch the admin SSH pubkey from Automation Variables (AZ_VM_ADMIN_SSH_PUBKEY).
    Azure Linux VM osProfile requires either at least one SSH key or password auth;
    we pick the key path so the VM has no interactive-login surface with a
    password. The private key is held by the operator; NSG denies inbound SSH
    anyway - the key exists to satisfy ARM validation, not for interactive use."""
    try:
        import automationassets
        return automationassets.get_automation_variable("AZ_VM_ADMIN_SSH_PUBKEY")
    except Exception:
        return os.environ.get("AZ_VM_ADMIN_SSH_PUBKEY", "")


def _provision_vm(run_id: str, load_mode: str, state_scope,
                  invocation_mode: str, resume_from_step: str = "") -> dict:
    """v32 §5.2.2: PUT a fresh Pipeline Run VM into snet-pipeline-compute.

    Async by nature: ARM returns a provisioning-state URL, not a
    completed VM. Runbook returns immediately after the PUT; cloud-init
    runs on the VM once it boots.
    """
    tok = _get_token("https://management.azure.com/")
    short_id = run_id.split("-")[-1][:8] if "-" in run_id else run_id[:8]
    vm_name = f"vm-chpipeline-{short_id}"
    subnet_id = (
        f"/subscriptions/{SUBSCRIPTION_ID}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.Network/virtualNetworks/{VM_VNET}"
        f"/subnets/{VM_SUBNET}"
    )
    mi_id = (
        f"/subscriptions/{SUBSCRIPTION_ID}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{VM_MI_NAME}"
    )
    # NIC creation is inline via ARM template style -- for a minimum viable
    # implementation, create the NIC in the same PUT chain.
    nic_name = f"{vm_name}-nic"
    # 1) Create NIC (dynamic private IP from subnet)
    nic_url = (
        f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
        f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/"
        f"networkInterfaces/{nic_name}?api-version=2023-09-01"
    )
    nic_body = {
        "location": VM_LOCATION,
        "tags": {
            "pipeline_run_id": run_id,
            "pipeline_name": PIPELINE_NAME,
            "env": ENV_PREFIX,
        },
        "properties": {
            "ipConfigurations": [{
                "name": "ipconfig1",
                "properties": {
                    "subnet": {"id": subnet_id},
                    "privateIPAllocationMethod": "Dynamic",
                },
            }],
        },
    }
    _put(nic_url, nic_body, tok)

    # 2) Create VM with the NIC attached + cloud-init user_data
    vm_url = (
        f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
        f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Compute/"
        f"virtualMachines/{vm_name}?api-version=2024-03-01"
    )
    user_data_b64 = _cloud_init_user_data(
        run_id, load_mode, state_scope, invocation_mode, resume_from_step
    )
    vm_body = {
        "location": VM_LOCATION,
        "tags": {
            "pipeline_run_id": run_id,
            "pipeline_name": PIPELINE_NAME,
            "env": ENV_PREFIX,
        },
        "identity": {
            "type": "UserAssigned",
            "userAssignedIdentities": {mi_id: {}},
        },
        "properties": {
            "hardwareProfile": {"vmSize": VM_SIZE},
            "storageProfile": {
                "imageReference": {
                    "publisher": "Canonical",
                    "offer": "ubuntu-24_04-lts",
                    "sku": "server",
                    "version": "latest",
                },
                "osDisk": {
                    "createOption": "FromImage",
                    "managedDisk": {"storageAccountType": "Premium_LRS"},
                },
            },
            "osProfile": {
                "computerName": vm_name,
                "adminUsername": "chpipeline",
                "linuxConfiguration": {
                    "disablePasswordAuthentication": True,
                    "ssh": {"publicKeys": [{
                        "path": "/home/chpipeline/.ssh/authorized_keys",
                        "keyData": _get_ssh_pubkey(),
                    }]},
                    "provisionVMAgent": True,
                },
                "customData": user_data_b64,
            },
            "networkProfile": {
                "networkInterfaces": [{
                    "id": (f"/subscriptions/{SUBSCRIPTION_ID}"
                           f"/resourceGroups/{RESOURCE_GROUP}"
                           f"/providers/Microsoft.Network/networkInterfaces/{nic_name}"),
                    "properties": {"primary": True},
                }],
            },
            "userData": user_data_b64,
        },
    }
    return _put(vm_url, vm_body, tok)


def _put(url: str, body: dict, tok: str) -> dict:
    req = urllib.request.Request(
        url,
        method="PUT",
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json"},
        data=json.dumps(body).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        # Read the ARM error body so the caller (and the log) sees which
        # resource + which permission failed, not just a bare HTTP code.
        try:
            body_txt = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            body_txt = ""
        raise RuntimeError(
            f"ARM PUT {url.rsplit('?', 1)[0]} -> HTTP {e.code}: {body_txt}"
        ) from e


def _atlas_resume_pipeline_cluster() -> dict:
    """v32 §5.2.2: POST Atlas Admin API to resume the pipeline cluster.

    Fires in parallel with VM create; the Controller polls the cluster's
    state on boot and typically finds it IDLE within a few seconds
    because the resume was kicked off earlier by this call.

    Returns the Atlas API response dict. On any error, logs and returns
    an empty dict (Controller will still poll and eventually succeed).
    """
    try:
        import automationassets  # only in AA sandbox
        atlas_project_id = automationassets.get_automation_variable("ATLAS_PROJECT_ID")
        atlas_pub = automationassets.get_automation_variable("ATLAS_PUBLIC_KEY")
        atlas_priv = automationassets.get_automation_variable("ATLAS_PRIVATE_KEY")
    except Exception:
        atlas_project_id = ATLAS_PROJECT_ID or os.environ.get("ATLAS_PROJECT_ID", "")
        atlas_pub = os.environ.get("ATLAS_PUBLIC_KEY", "")
        atlas_priv = os.environ.get("ATLAS_PRIVATE_KEY", "")
    if not atlas_project_id:
        log("atlas_resume_skipped_no_project_id")
        return {}
    if not atlas_pub or not atlas_priv:
        log("atlas_resume_skipped_no_credentials")
        return {}
    # Atlas Admin API keys authenticate with HTTP Digest, NOT Basic. The
    # `requests` library's HTTPDigestAuth handles the challenge/response
    # dance correctly out-of-the-box; urllib's handler does not (it silently
    # fails to retry if the server's challenge shape does not match its
    # exact expectations). Legacy atlas_cluster_manager.py uses this same
    # pattern and works from Function Apps.
    import requests
    from requests.auth import HTTPDigestAuth
    url = (f"{ATLAS_ADMIN_BASE}/groups/{atlas_project_id}"
           f"/clusters/{ATLAS_PIPELINE_CLUSTER}")
    r = requests.patch(
        url,
        auth=HTTPDigestAuth(atlas_pub, atlas_priv),
        headers={
            "Content-Type": "application/vnd.atlas.2024-08-05+json",
            "Accept": "application/vnd.atlas.2024-08-05+json",
        },
        json={"paused": False},
        timeout=30,
    )
    r.raise_for_status()
    return r.json() if r.content else {}


def _provision_vm_and_wake_mongo_in_parallel(
    run_id: str, load_mode: str, state_scope,
    invocation_mode: str, resume_from_step: str = "",
) -> dict:
    """v32 §5.2.2 par-block: fire VM create AND Atlas resume in parallel.

    Runbook returns as soon as both API calls have been dispatched (not
    when the VM boots or the cluster is IDLE -- those are Controller's
    responsibility to poll).
    """
    import threading

    results = {"vm": None, "atlas": None, "vm_error": None, "atlas_error": None}

    def _run_vm():
        try:
            results["vm"] = _provision_vm(
                run_id, load_mode, state_scope, invocation_mode, resume_from_step
            )
        except Exception as exc:  # noqa: BLE001
            results["vm_error"] = f"{type(exc).__name__}: {exc}"
            log("vm_provision_error", error=results["vm_error"])

    def _run_atlas():
        try:
            results["atlas"] = _atlas_resume_pipeline_cluster()
        except Exception as exc:  # noqa: BLE001
            results["atlas_error"] = f"{type(exc).__name__}: {exc}"
            log("atlas_resume_error", error=results["atlas_error"])

    t_vm = threading.Thread(target=_run_vm, daemon=False)
    t_atlas = threading.Thread(target=_run_atlas, daemon=False)
    t_vm.start()
    t_atlas.start()
    t_vm.join(timeout=90)
    t_atlas.join(timeout=90)
    if results["vm_error"]:
        raise RuntimeError(f"VM provisioning failed: {results['vm_error']}")
    return results


# ============================================================================
# Webhook input parsing
# ============================================================================
def _parse_webhook_input() -> dict:
    """Webhook payload discovery across every mechanism Azure Automation
    might use for Python-3 runbooks:
      - WEBHOOKDATA env var (documented Python-3 mechanism)
      - sys.argv[1] as a JSON string (PowerShell convention)
      - other env vars containing WEBHOOK/AUTOMATION/TRIGGER/INPUT
      - stdin (non-blocking read)
    Emits a diagnostic event listing every source and their (truncated)
    values so the payload location can be pinpointed at runtime.
    Returns the parsed RequestBody dict (POST body), or {} when the
    runbook was not triggered by a webhook."""
    # Diagnostic -- dump every candidate source with truncated values.
    candidates: dict = {"argv": [str(a)[:400] for a in sys.argv]}
    for k, v in os.environ.items():
        ku = k.upper()
        if any(t in ku for t in ("WEBHOOK", "AUTOMATION", "TRIGGER", "INPUT", "RUNBOOK", "JOB")):
            candidates[f"env:{k}"] = (v or "")[:400]
    # Try non-blocking stdin peek.
    try:
        import select as _select
        r, _, _ = _select.select([sys.stdin], [], [], 0.05)
        if r:
            stdin_data = sys.stdin.read(4000)
            candidates["stdin"] = stdin_data[:400]
    except Exception as _e:
        candidates["stdin_err"] = str(_e)[:200]
    log("webhook_payload_discovery", **{k: v for k, v in list(candidates.items())[:20]})

    # AA Python-3 sandbox delivers the webhook envelope via sys.argv, not
    # WEBHOOKDATA (which is the PowerShell mechanism). It also does NOT
    # quote-protect the arg, so the payload arrives whitespace-split across
    # sys.argv[1:]. Reconstruct with a single space then extract the JSON
    # value of the RequestBody key via balanced-brace parsing (the envelope
    # is PowerShell-hashtable-shaped -- unquoted keys, comma-separated --
    # so json.loads on the whole thing fails; only RequestBody's VALUE is
    # proper JSON).
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
        except Exception:
            return {}
    start = idx + len(marker)
    while start < len(raw) and raw[start] != "{":
        start += 1
    if start >= len(raw):
        return {}
    depth = 0
    end = start
    for i in range(start, len(raw)):
        c = raw[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if depth != 0:
        return {}
    body_json = raw[start:end + 1]
    try:
        parsed = json.loads(body_json)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _resolve_state_scope(raw):
    """Accept 'ALL' (str), ['ALL'], ['VT','DE'], etc. Always return a list."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        if raw.strip().startswith("["):
            try:
                v = json.loads(raw)
                if isinstance(v, list):
                    return v
            except Exception:
                pass
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return ["ALL"]


# ============================================================================
# Main
# ============================================================================
def main() -> int:
    invocation_mode = INVOCATION_MODE
    webhook_body = _parse_webhook_input()
    if webhook_body:
        invocation_mode = webhook_body.get("invocation_mode", "webhook")
    load_mode = webhook_body.get("load_mode", LOAD_MODE_DEFAULT) if webhook_body else LOAD_MODE_DEFAULT
    state_scope = _resolve_state_scope(
        webhook_body.get("state_scope") if webhook_body else STATE_SCOPE_DEFAULT
    )
    resume_from_step = (
        (webhook_body.get("resume_from_step") or "").strip()
        if webhook_body else ""
    )

    log("runbook_start",
        pipeline=PIPELINE_NAME,
        env=ENV_PREFIX,
        invocation_mode=invocation_mode,
        load_mode=load_mode,
        state_scope=state_scope,
        resume_from_step=resume_from_step or None)

    # Fresh run_id
    now = datetime.datetime.utcnow()
    run_id = f"prov-{now.strftime('%Y-%m-%dT%H-%M-%SZ')}-{uuid.uuid4().hex[:6]}"
    log("run_id_generated", run_id=run_id)

    # Sentinel for the finally block. When True this invocation is a
    # duplicate that lost the serialization race; the live run owns every
    # shared resource and any teardown here MUST be a no-op.
    is_duplicate_abend = False
    # The AA sandbox trust store lacks Atlas's Root CA. Point pymongo TLS
    # at certifi's bundle so front-cluster TLS handshakes succeed.
    try:
        import certifi as _certifi
        _mongo_tls_ca = _certifi.where()
    except ImportError:
        _mongo_tls_ca = None
    try:
        # Config + manifest -- against the ALWAYS-UP front cluster.
        try:
            import pymongo  # provided by AA Python 3 package
            conn = _get_mongo_conn_string()
            log("mongo_secret_fetched", vault_uri=KEY_VAULT_URI)
            _mongo_kwargs = {"serverSelectionTimeoutMS": 15000}
            if _mongo_tls_ca:
                _mongo_kwargs["tlsCAFile"] = _mongo_tls_ca
            # Atlas SRV lookups fail from the Automation sandbox because its
            # resolver cannot answer _mongodb._tcp.<domain> SRV queries. Try
            # a direct connect first; on SRV failure, translate the URI to
            # its non-SRV equivalent via DNS-over-HTTPS and retry.
            try:
                mongo = pymongo.MongoClient(conn, **_mongo_kwargs)
                mongo.admin.command("ping")
            except Exception:
                direct = _srv_to_direct_mongo_uri(conn)
                log("mongo_srv_bypass_active", direct_hostcount=direct.count(",") + 1
                    if direct.startswith("mongodb://") else 0)
                mongo = pymongo.MongoClient(direct, **_mongo_kwargs)
                mongo.admin.command("ping")
            _activate_mongo_logging(mongo)
            log("mongo_log_activated",
                collection="chathealthyfrontend.logFileCollection")
            live = _find_live_pipeline_run(mongo)
            if live is not None:
                is_duplicate_abend = True
                log("pipeline_already_running_abend",
                    attempted_run_id=run_id,
                    active_reservation_job_id=(
                        live.get("_id") or live.get("job_id")
                    ),
                    reservation_requester=live.get("requester"),
                    reservation_start_time=str(live.get("start_time")),
                    reservation_class=live.get("reservation_class"))
                return 1
            config = _read_pipeline_config(mongo)
            log("config_read",
                config_keys=list(config.keys()),
                found=bool(config))
            _write_run_manifest(mongo, run_id, load_mode, state_scope,
                                invocation_mode, config)
            log("run_manifest_written", run_id=run_id)
        except Exception as exc:
            # Front-cluster Mongo is critical -- coordination state
            # (serialization guard, run manifest, config) lives there and
            # Control cannot proceed without it. Abort so the AA job history
            # shows the failure and Control is NOT started.
            log("mongo_step_failed_abort",
                error_type=type(exc).__name__,
                error_msg=str(exc)[:1500],
                traceback=traceback.format_exc()[-2000:])
            return 1

        # v32 §5.2.2: provision VM AND wake Mongo in parallel.
        try:
            result = _provision_vm_and_wake_mongo_in_parallel(
                run_id, load_mode, state_scope, invocation_mode, resume_from_step
            )
            vm_id = ((result.get("vm") or {}).get("id")) if result.get("vm") else None
            log("vm_provision_dispatched_and_mongo_woke_in_parallel",
                run_id=run_id,
                vm_id=vm_id,
                atlas_resume_error=result.get("atlas_error"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:1500]
            except Exception:
                pass
            log("vm_provision_failed",
                run_id=run_id,
                http_code=exc.code,
                reason=exc.reason,
                body=body)
            return 1
        except Exception as exc:
            log("vm_provision_failed",
                run_id=run_id,
                error_type=type(exc).__name__,
                error_msg=str(exc),
                traceback=traceback.format_exc()[-2000:])
            return 1

        log("runbook_exit", run_id=run_id)
        return 0
    finally:
        # Teardown clause. When is_duplicate_abend is True the live run
        # owns every shared resource (source blob lease, run manifest,
        # cluster reservation, Control ACA execution); the duplicate MUST
        # leave them alone. Any release code added here later MUST gate
        # on `not is_duplicate_abend`.
        if is_duplicate_abend:
            log("duplicate_abend_teardown_skipped", run_id=run_id)


if __name__ == "__main__":
    sys.exit(main())
