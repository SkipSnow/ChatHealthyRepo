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
import subprocess
import traceback
import urllib.error
import urllib.request
import uuid

# Ensure required Azure SDK packages are installed in Automation runtime
_REQUIRED_PACKAGES = ["azure-identity", "azure-keyvault-secrets", "pymongo", "cryptography"]
for _pkg in _REQUIRED_PACKAGES:
    try:
        __import__(_pkg.replace("-", "_"))
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", _pkg])

from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities
from chathealthy_frontend_lib.exceptions import ChatHealthyException

# All pipeline metadata lives in one database. This runbook ships to Azure
# Automation standalone, so it carries the constant rather than importing
# pipeline_db, which is not deployed alongside it.
PIPELINE_ADMIN_DB = "pipelineAdmin"



# CHLS wants these in os.environ. Set them BEFORE the first log() call so
# CH_LOG_DESTINATION drives log output to stderr (AA captures stderr into
# the ARM job exception field, which is the only surface operators can
# read on a failed run) and CH_SPACE_NAME + ENV_PREFIX satisfy the Mongo
# handler prerequisite so it wires as soon as MONGO_connectionString
# gets published from the KV fetch further down.
os.environ.setdefault("CH_LOG_DESTINATION", "stderr,mongo")
os.environ.setdefault("CH_SPACE_NAME", "runbook")
os.environ.setdefault("ENV_PREFIX",
                      os.environ.get("AUTOMATION_ENV_PREFIX", "dev"))
os.environ.setdefault("CH_COMPONENT", "provider_pipeline_runbook")


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
VM_SIZE = os.environ.get("AUTOMATION_VM_SIZE", "Standard_D32s_v6")
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
    "ChatHealthyDataPipelines",
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
# Structured event logging.
# Every event goes through ChatHealthyLoggingService - the canonical logger
# per Rule-005 - whose MongoLogHandler writes each record to Pipelines.Log_
# {env}. No parallel log path; no direct collection write from this module.
# ============================================================================
def log(event: str, **fields):
    """Emit one structured event via ChatHealthyLoggingService.info(). The
    service's MongoLogHandler persists the record to Pipelines.Log_{env};
    the file/stderr handler mirrors it for job-stream visibility. On
    Mongo-write failure the service raises and this stage abends per the
    'if you can't log to Mongo you die' policy."""
    now = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    record = {
        "ts": now,
        "runbook": PIPELINE_NAME + "_pipeline_runbook",
        "hostname": _HOSTNAME,
        "event": event,
    }
    record.update(fields)
    ChatHealthyLoggingService().info(json.dumps(record, default=str))


# ============================================================================
# Mongo config + run manifest -- reads Mongo conn string from Key Vault
# ============================================================================
def _get_mongo_conn_string() -> str:
    """Fetch front-cluster connection string from Key Vault. Trigger-tier
    coordination (serialization guard, pipeline.runs manifest, pipeline.config
    read) lives on the ALWAYS-UP front cluster, not the pipeline cluster
    which is paused-by-default between runs (Skip 2026-07-18)."""
    name = os.environ.get("MONGO_SECRET_NAME", "MONGO-connectionString")
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
        raise ChatHealthyException(mode="runtime_error", message=f"DoH SRV lookup returned no hosts for {host_only}")
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
    coll = mongo[PIPELINE_ADMIN_DB]["pipeline.config"]
    doc = coll.find_one({"pipeline_name": PIPELINE_NAME, "env": ENV_PREFIX}) or {}
    return doc


# Same-pipeline lock TTL. Longer than any realistic pipeline run so a
# crashed VM never wedges future fires forever, but long enough that a
# healthy long-running fire never expires mid-flight.
_PIPELINE_LOCK_TTL_HOURS = 12


def _pipeline_lock_id(pipeline_name: str) -> str:
    """Deterministic _id for the same-pipeline mutual-exclusion lock.
    Same pipeline_name -> same _id -> Mongo enforces at-most-one-active
    via its natural _id uniqueness. Distinct from reservation rows
    (which have arbitrary _ids) so the two concerns don't collide."""
    return f"pipeline_lock:{pipeline_name}"


def _acquire_pipeline_lock(
    mongo, pipeline_name: str, run_id: str, vm_name: str,
) -> dict | None:
    """Atomically acquire the per-pipeline lock. Returns None on success
    (we hold the lock); returns the blocking lock document if another
    fire already holds it (caller MUST abend without touching any
    shared resource).

    Uses insert_one on a deterministic _id so Mongo's natural _id
    uniqueness gives us the atomic acquire -- no read-check-write race.
    Two webhooks arriving at the same instant -> exactly one insert
    wins, the other gets DuplicateKeyError -> abend. This is the
    correctness contract; the requirement is 'no pipeline can run
    twice concurrently.'

    Stale-lock self-heal: if an existing lock's expires_at is in the
    past (crashed VM never released), delete it and retry the insert
    once. Guards against wedging the pipeline forever on a crash."""
    from datetime import datetime, timedelta, timezone
    from pymongo.errors import DuplicateKeyError
    coll = mongo[PIPELINE_ADMIN_DB]["cluster_lifecycle"]
    now = datetime.now(timezone.utc)
    doc = {
        "_id": _pipeline_lock_id(pipeline_name),
        "kind": "pipeline_lock",
        "pipeline_name": pipeline_name,
        "run_id": run_id,
        "vm_name": vm_name,
        "acquired_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=_PIPELINE_LOCK_TTL_HOURS)).isoformat(),
    }
    try:
        coll.insert_one(doc)
        return None
    except DuplicateKeyError:
        existing = coll.find_one({"_id": doc["_id"]})
        if existing:
            exp = existing.get("expires_at") or ""
            if exp and exp < now.isoformat():
                # Stale lock (crashed VM never released). Delete + retry
                # once. If a second concurrent fire grabbed it between
                # our delete and insert, we lose the race legitimately.
                coll.delete_one({"_id": doc["_id"], "run_id": existing.get("run_id")})
                try:
                    coll.insert_one(doc)
                    return None
                except DuplicateKeyError:
                    return coll.find_one({"_id": doc["_id"]})
        return existing


def _release_pipeline_lock(mongo, pipeline_name: str, run_id: str) -> None:
    """Release the per-pipeline lock iff we own it (matched by run_id).
    Idempotent: safe to call in a finally block even if we never
    acquired (a different run_id owns the lock now -> no-op)."""
    coll = mongo[PIPELINE_ADMIN_DB]["cluster_lifecycle"]
    coll.delete_one({"_id": _pipeline_lock_id(pipeline_name), "run_id": run_id})


RESERVATION_TTL_HOURS = 10


def _reservation_short_id(run_id: str) -> str:
    """Same VM-name suffix _provision_vm computes; used to correlate the
    reservation record with the eventual VM."""
    return run_id.split("-")[-1][:8] if "-" in run_id else run_id[:8]


def _create_reservation(mongo, run_id: str, vm_name: str) -> None:
    """Write the pipeline-run reservation on the front cluster's
    admin.cluster_lifecycle collection. The reservation is the primary
    contract: Controller inherits it and cancels in its main.finally;
    reservation_reaper reaps expiry_at < now if Controller never gets
    that far. TTL is 10 hours (outer safety envelope, not a startup
    timeout)."""
    coll = mongo[PIPELINE_ADMIN_DB]["cluster_lifecycle"]
    now = datetime.datetime.utcnow()
    expiry = now + datetime.timedelta(hours=RESERVATION_TTL_HOURS)
    coll.replace_one(
        {"_id": run_id},
        {
            "_id": run_id,
            "run_id": run_id,
            "vm_name": vm_name,
            "cluster_name": ATLAS_PIPELINE_CLUSTER,
            "requester": "provider_pipeline_runbook",
            "start_time": now,
            "expiry_at": expiry,
            "reservation_class": "pipeline_run",
            "pipeline_name": PIPELINE_NAME,
            "status": "active",
        },
        upsert=True,
    )


def _cancel_reservation(mongo, run_id: str) -> None:
    """Delete the reservation for a specific run_id. Idempotent."""
    coll = mongo[PIPELINE_ADMIN_DB]["cluster_lifecycle"]
    coll.delete_one({"_id": run_id})


def _delete_vm_via_arm(vm_name: str) -> None:
    """Fire-and-forget VM delete via ARM REST. Silent on 404 (already gone)
    and 409 (delete queued while another operation is in flight; ARM
    completes the delete once the in-flight op finishes). Raises on
    other HTTP errors so the caller can log + swallow."""
    url = (
        f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
        f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Compute/"
        f"virtualMachines/{vm_name}?api-version=2024-03-01"
    )
    tok = _get_token("https://management.azure.com/")
    req = urllib.request.Request(url, method="DELETE",
                                 headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        if e.code in (404, 409):
            return
        raise


def _runbook_error_teardown(mongo, run_id: str, vm_name: str,
                            reservation_created: bool,
                            vm_provisioned: bool) -> None:
    """Called by the runbook on any error path after either the VM PUT
    landed or the reservation was written. Cancels the reservation and
    deletes the VM so nothing leaks. Idempotent + best-effort - each
    step is wrapped and its failure is logged but does not block the
    others."""
    if reservation_created:
        try:
            _cancel_reservation(mongo, run_id)
            log("runbook_teardown_reservation_cancelled", run_id=run_id)
        except Exception as exc:
            log("runbook_teardown_reservation_cancel_failed",
                run_id=run_id, error=str(exc)[:500])
    if vm_provisioned:
        try:
            _delete_vm_via_arm(vm_name)
            log("runbook_teardown_vm_delete_dispatched",
                run_id=run_id, vm_name=vm_name)
        except Exception as exc:
            log("runbook_teardown_vm_delete_failed",
                run_id=run_id, vm_name=vm_name, error=str(exc)[:500])


def _raise_missing_data_version(webhook_body) -> None:
    """Raise-only helper: rejects a webhook payload that omits data_version.
    Rule-005 keeps the raise separate from the caller's log call."""
    raise ChatHealthyException(
        mode="value_error",
        message=(
            "provider_pipeline_runbook: webhook payload MUST include "
            "data_version as int >= 1. Fire again with a valid value."
        ),
        webhook_body_head=str(webhook_body)[:400],
    )


def _write_run_manifest(mongo, run_id: str, load_mode: str,
                        state_scope, invocation_mode: str,
                        config: dict) -> None:
    """LLD §2.6 step 3: fresh manifest in chathealthyfrontend.pipeline.runs."""
    coll = mongo[PIPELINE_ADMIN_DB]["pipeline.runs"]
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
                          invocation_mode: str, resume_from_step: str,
                          data_version: int,
                          google_maps_enabled: bool = False) -> str:
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
# Override the default Azure Ubuntu mirror (azure.archive.ubuntu.com), which is
# intermittently unreachable from snet-pipeline-compute on port 80 and has
# caused repeated cloud-init failures. Canonical's own archive.ubuntu.com is on
# a different CDN with a different network path from our subnet.
apt:
  primary:
    - arches: [default]
      uri: https://archive.ubuntu.com/ubuntu
  security:
    - arches: [default]
      uri: https://security.ubuntu.com/ubuntu
package_update: true
package_upgrade: false
packages:
  - docker.io
runcmd:
  - |
    #!/bin/bash
    set -eux
    exec >> /var/log/chpipeline-cloud-init.log 2>&1
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
    # Prepare scratch directory on the VM's local NVMe temp disk. Dsv6-family
    # VMs ship with ~220 GB of local NVMe mounted at /mnt/resource by waagent
    # (free with the SKU, wiped on VM stop). We bind-mount this into the
    # container as /scratch and set TMPDIR=/scratch so all Python tempfile
    # writes (NPPES download, zip extract, derived sources) land on NVMe
    # rather than the 30 GB Premium_LRS OS disk. Prior runs hit
    # [Errno 28] No space left on device on the OS disk with the ~5 GB
    # NPPES extract; with /scratch the working set has 22x headroom.
    mkdir -p /mnt/resource/pipeline-scratch
    chmod 1777 /mnt/resource/pipeline-scratch
    # Run Controller. Container is --rm so filesystem cleans up on exit.
    # Controller's finally block fires `az vm delete` on AZURE_VM_NAME on
    # normal exit. But if Controller aborts BEFORE main() runs (e.g.,
    # bootstrap observability gate raises), no finally fires. The cloud-init
    # safety-net after docker run ALWAYS fires az vm delete, idempotent:
    # a duplicate DELETE 404s if Controller already fired it.
    set +e
    docker run --rm --network host \\
      -v /mnt/resource/pipeline-scratch:/scratch \\
      -e TMPDIR=/scratch \\
      -e CHATHEALTHY_NODE_IDENTITY='pipeline-control' \\
      -e CH_SPACE_NAME='control' \\
      -e CH_LOG_DESTINATION='stderr,mongo' \\
      -e CH_LOG_LEVEL='DEBUG' \\
      -e CH_COMPONENT='provider_pipeline_control' \\
      -e PIPELINE_LOG_ACCOUNT_URL='https://stchpipelinedev.blob.core.windows.net' \\
      -e RUN_ID='{run_id}' \\
      -e DATA_VERSION='{data_version}' \\
      -e GOOGLE_MAPS_ENABLED='{"1" if google_maps_enabled else "0"}' \\
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
    DOCKER_EXIT=$?
    echo "chpipeline: docker run exit=$DOCKER_EXIT $(date -u +%FT%TZ)"
    set -e
    # Farewell VM delete. Fires regardless of docker exit code:
    #   - Controller normal exit (0): its finally already dispatched delete;
    #     this second DELETE 404s harmlessly.
    #   - Controller aborted (non-zero): finally never ran; this is the
    #     only VM-teardown path. Prevents idle VMs from accumulating.
    echo "chpipeline: firing farewell az vm delete $(date -u +%FT%TZ)"
    az vm delete --resource-group {RESOURCE_GROUP} --name {vm_name_for_farewell} --yes --no-wait || true
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
                  invocation_mode: str, resume_from_step: str = "",
                  data_version: int = 0,
                  google_maps_enabled: bool = False) -> dict:
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
        run_id, load_mode, state_scope, invocation_mode, resume_from_step,
        data_version, google_maps_enabled,
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
                    # Cascade the OS disk with the VM. Without this, an
                    # az vm delete leaves the Unattached OS disk behind,
                    # each ~30 GB Premium_LRS. Accumulated 35 orphans by
                    # 2026-07-29 costing ~200 USD/month. Watchdog also
                    # reaps orphans as a safety net (see
                    # watchdog_runbook._reap_orphan_disks), but this
                    # closes the leak at the source for every future run.
                    "deleteOption": "Delete",
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
                    "properties": {
                        "primary": True,
                        # Cascade NIC with VM delete. Without this, every
                        # `az vm delete` leaves the NIC behind as an orphan
                        # (same class of leak as the osDisk deleteOption
                        # above, noted 2026-08-02 on run 353476 -- the
                        # vm-chpipeline-353476-nic survived teardown).
                        # Watchdog also reaps orphan NICs as a safety net,
                        # but this closes the leak at the source.
                        "deleteOption": "Delete",
                    },
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
        raise ChatHealthyException(mode="runtime_error", message=f"ARM PUT {url.rsplit('?', 1)[0]} -> HTTP {e.code}: {body_txt}"
        ) from e


def _atlas_resume_pipeline_cluster() -> dict:
    """v32 §5.2.2 (revised v35): POST Atlas Admin API to resume the
    pipeline cluster, then POLL until the cluster reports stateName=IDLE.

    No silent-skip. Missing credentials or project id RAISE
    ChatHealthyException so the caller aborts the run visibly instead of
    marching on with a paused cluster. Returns the final Atlas GET
    response once the cluster is IDLE.
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
    missing = []
    if not atlas_project_id:
        missing.append("ATLAS_PROJECT_ID")
    if not atlas_pub:
        missing.append("ATLAS_PUBLIC_KEY")
    if not atlas_priv:
        missing.append("ATLAS_PRIVATE_KEY")
    if missing:
        raise ChatHealthyException(
            mode="atlas_resume_env_unset",
            message=(
                "Atlas Admin credentials missing from Automation Variables: "
                f"{', '.join(missing)}. Cannot resume pipeline cluster "
                f"{ATLAS_PIPELINE_CLUSTER}; run must abort."
            ),
            component="ProviderPipelineRunbook",
            missing=",".join(missing),
        )
    import requests
    from requests.auth import HTTPDigestAuth
    base = (f"{ATLAS_ADMIN_BASE}/groups/{atlas_project_id}"
            f"/clusters/{ATLAS_PIPELINE_CLUSTER}")
    headers = {
        "Content-Type": "application/vnd.atlas.2024-08-05+json",
        "Accept": "application/vnd.atlas.2024-08-05+json",
    }
    auth = HTTPDigestAuth(atlas_pub, atlas_priv)
    # 1. Dispatch resume PATCH.
    r = requests.patch(base, auth=auth, headers=headers,
                       json={"paused": False}, timeout=30)
    r.raise_for_status()
    log("atlas_resume_patch_dispatched", cluster=ATLAS_PIPELINE_CLUSTER)
    # 2. Poll GET until stateName=IDLE (cluster is up and accepting writes).
    #    No timeout cap: Atlas cluster wake is Atlas's promise, not ours;
    #    the AA sandbox's 3-hour runbook fair-share limit is the outer
    #    envelope. If Atlas is genuinely stuck the runbook surfaces that
    #    to the operator by hitting the AA limit rather than by faking
    #    an operator-authored cap.
    import time as _time
    poll_start = _time.time()
    while True:
        g = requests.get(base, auth=auth, headers=headers, timeout=30)
        g.raise_for_status()
        state = (g.json() or {}).get("stateName", "")
        if state == "IDLE":
            log("atlas_resume_cluster_idle",
                cluster=ATLAS_PIPELINE_CLUSTER,
                waited_s=round(_time.time() - poll_start, 1))
            return g.json()
        _time.sleep(15)


def _provision_vm_and_wake_mongo_in_parallel(
    run_id: str, load_mode: str, state_scope,
    invocation_mode: str, resume_from_step: str = "",
    data_version: int = 0,
    google_maps_enabled: bool = False,
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
                run_id, load_mode, state_scope, invocation_mode, resume_from_step,
                data_version, google_maps_enabled,
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
        raise ChatHealthyException(mode="runtime_error", message=f"VM provisioning failed: {results['vm_error']}")
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
    # Hoist Mongo secret fetch AND SRV-to-direct conversion to the top so
    # CHLS's Mongo handler can wire on the first log() call below. The AA
    # Python 3 sandbox cannot resolve `_mongodb._tcp.*` SRV records, so
    # the SRV-form URI from KV MUST be converted to its non-SRV direct
    # form here via DNS-over-HTTPS before any CHLS.info() call constructs
    # a MongoClient. Without this, log() -> CHLS -> mongo_utilities ->
    # MongoClient(srv_uri) -> pymongo's SRV resolver fails and the
    # runbook crashes on the very first log call.
    os.environ["MONGO_connectionString"] = _srv_to_direct_mongo_uri(
        _get_mongo_conn_string()
    )

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
    # Mandatory data_version - no default. Operator's fire_provider_
    # pipeline_test enforces this on the payload side; runbook enforces
    # it on the receive side. Fail loud if missing.
    dv_raw = (webhook_body or {}).get("data_version")
    try:
        data_version = int(dv_raw)
    except (TypeError, ValueError):
        data_version = 0
    if data_version < 1:
        log("runbook_reject_no_data_version", webhook_body=str(webhook_body)[:400])
        _raise_missing_data_version(webhook_body)
    # Publish to env so cloud-init template injects it into docker run
    # -e DATA_VERSION=<n>, which the Controller argparse and CHLS both
    # read.
    os.environ["DATA_VERSION"] = str(data_version)

    # Optional: google_maps_enabled toggles the paid stage in the county
    # cascade (LLD 4.13 stage 4). Off by default; on when webhook payload
    # says {1,true,yes} or bool True. Published via env so cloud-init
    # injects -e GOOGLE_MAPS_ENABLED=<0|1> that the Controller argparse
    # picks up.
    gm_raw = (webhook_body or {}).get("google_maps_enabled")
    if isinstance(gm_raw, bool):
        google_maps_enabled = gm_raw
    else:
        google_maps_enabled = str(gm_raw or "").strip().lower() in ("1", "true", "yes")
    os.environ["GOOGLE_MAPS_ENABLED"] = "1" if google_maps_enabled else "0"

    log("runbook_start",
        pipeline=PIPELINE_NAME,
        env=ENV_PREFIX,
        invocation_mode=invocation_mode,
        load_mode=load_mode,
        state_scope=state_scope,
        data_version=data_version,
        google_maps_enabled=google_maps_enabled,
        resume_from_step=resume_from_step or None)

    # Fresh run_id
    now = datetime.datetime.utcnow()
    run_id = f"prov-{now.strftime('%Y-%m-%dT%H-%M-%SZ')}-{uuid.uuid4().hex[:6]}"
    # Publish RUN_ID into env so subsequent log() calls populate job_id
    # on the CHLS Mongo document (Log_{env}.job_id). ChatHealthyLoggingService
    # reads os.environ.get('RUN_ID') at emit time.
    os.environ["RUN_ID"] = run_id
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
            # Route through ChatHealthyMongoUtilities. It reads the URI
            # from an env var; publish the KV-fetched conn string under a
            # stable name before instantiating. The SRV-bypass retry path
            # publishes the direct-mode URI under the same name.
            try:
                os.environ["MONGO_FRONTEND_connectionString"] = conn
                mongo = ChatHealthyMongoUtilities().getConnection(
                    "pipelineEditor", "frontEnd"
                )
                mongo.admin.command("ping")
            except Exception:
                direct = _srv_to_direct_mongo_uri(conn)
                log("mongo_srv_bypass_active",
                    direct_hostcount=direct.count(",") + 1 if direct.startswith("mongodb://") else 0)
                os.environ["MONGO_FRONTEND_connectionString"] = direct
                mongo = ChatHealthyMongoUtilities().getConnection(
                    "pipelineEditor", "frontEnd"
                )
                mongo.admin.command("ping")
            log("mongo_connected", cluster="chathealthydatapipelines")
            # Same-pipeline mutual exclusion. Atomic acquire on
            # _id="pipeline_lock:<pipeline_name>". Only blocks fires
            # for the SAME pipeline_name -- different pipelines and
            # manual DB-awake reservations are transparent.
            _vm_name_for_lock = f"vm-chpipeline-{_reservation_short_id(run_id)}"
            blocking = _acquire_pipeline_lock(
                mongo, PIPELINE_NAME, run_id, _vm_name_for_lock,
            )
            if blocking is not None:
                is_duplicate_abend = True
                log("pipeline_already_running_abend",
                    attempted_run_id=run_id,
                    pipeline_name=PIPELINE_NAME,
                    live_run_id=blocking.get("run_id"),
                    live_vm_name=blocking.get("vm_name"),
                    live_acquired_at=str(blocking.get("acquired_at")),
                    live_expires_at=str(blocking.get("expires_at")))
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
        # Lock lifetime handoff: the runbook acquired the pipeline_lock
        # above; on successful VM PUT, ownership transfers to the
        # Controller inside the VM (its quiesce step releases the lock
        # at pipeline end). If the runbook fails BEFORE VM PUT succeeds,
        # runbook_owns_lock_release stays True and the finally block
        # releases the lock so a stuck lock doesn't wedge future fires.
        runbook_owns_lock_release = True
        reservation_created = False
        vm_provisioned = False
        vm_name_for_teardown = f"vm-chpipeline-{_reservation_short_id(run_id)}"
        try:
            result = _provision_vm_and_wake_mongo_in_parallel(
                run_id, load_mode, state_scope, invocation_mode, resume_from_step,
                data_version, google_maps_enabled,
            )
            vm_id = ((result.get("vm") or {}).get("id")) if result.get("vm") else None
            vm_provisioned = True
            # VM is up; Controller now owns lock release via its
            # _quiesce_mongo_state step. Runbook must NOT release.
            runbook_owns_lock_release = False
            log("vm_provision_dispatched_and_mongo_woke_in_parallel",
                run_id=run_id,
                vm_id=vm_id,
                atlas_resume_error=result.get("atlas_error"))
            # Reservation on the front cluster's admin.cluster_lifecycle.
            # Written AFTER VM PUT succeeds so the reservation always
            # references a real VM. Controller inherits and cancels in
            # its finally; ReservationReaper cleans up if expiry_at hits.
            _create_reservation(mongo, run_id, vm_name_for_teardown)
            reservation_created = True
            log("reservation_created",
                run_id=run_id,
                vm_name=vm_name_for_teardown,
                ttl_hours=RESERVATION_TTL_HOURS)
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
            _runbook_error_teardown(mongo, run_id, vm_name_for_teardown,
                                    reservation_created, vm_provisioned)
            return 1
        except Exception as exc:
            log("vm_provision_failed",
                run_id=run_id,
                error_type=type(exc).__name__,
                error_msg=str(exc),
                traceback=traceback.format_exc()[-2000:])
            _runbook_error_teardown(mongo, run_id, vm_name_for_teardown,
                                    reservation_created, vm_provisioned)
            return 1

        log("runbook_exit", run_id=run_id)
        return 0
    finally:
        # Teardown clause. Three cases:
        #   (a) is_duplicate_abend -- another live run owns the lock;
        #       this invocation's acquire failed so it never owned the
        #       lock. Do nothing.
        #   (b) runbook_owns_lock_release=True -- runbook acquired the
        #       lock but failed before VM PUT succeeded. Release now so
        #       a stuck lock doesn't wedge future fires.
        #   (c) runbook_owns_lock_release=False -- VM PUT succeeded;
        #       Controller inside the VM owns lock release via its
        #       _quiesce_mongo_state step. Runbook must NOT release
        #       here or the ~25min VM run would be unlocked.
        if is_duplicate_abend:
            log("duplicate_abend_teardown_skipped", run_id=run_id)
        elif runbook_owns_lock_release:
            try:
                if mongo is not None:
                    _release_pipeline_lock(mongo, PIPELINE_NAME, run_id)
                    log("pipeline_lock_released_runbook_side",
                        pipeline_name=PIPELINE_NAME, run_id=run_id,
                        reason="runbook_failed_before_vm_handoff")
            except Exception as exc:
                log("pipeline_lock_release_error",
                    pipeline_name=PIPELINE_NAME, run_id=run_id,
                    error=str(exc)[:400])
        else:
            log("pipeline_lock_ownership_handed_to_vm",
                pipeline_name=PIPELINE_NAME, run_id=run_id,
                vm_name=vm_name_for_teardown)


if __name__ == "__main__":
    sys.exit(main())
