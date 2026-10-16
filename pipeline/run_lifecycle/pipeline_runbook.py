# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
"""pipeline_runbook.py -- the ONE generic pipeline trigger runbook (gateway).

Runs in Azure Automation as a Python 3 runbook, fired by schedule or webhook.
It is the single per-pipeline trigger runbook: one entry point that reads the
webhook's `pipeline_name` field and dispatches to the matching per-pipeline
trigger flow:

    absent / null / empty / missing  -> 'provider'   (pipeline 0, the default;
                                                       legacy callers that send
                                                       NO pipeline_name route
                                                       here, exactly as before)
    'medicare'                       -> 'medicare'    (pipeline 1)
    anything else                    -> rejected with a ChatHealthyException,
                                        logged, never silently defaulted.

WHY TWO FLOWS RATHER THAN ONE SHARED FLOW. The two trigger tiers are NOT
behaviourally identical, so they could not be folded into a single shared flow
without changing one pipeline's behaviour. The genuinely-identical pieces are
merged once below (editor-credential acquisition, ARM OAuth token, ARM PUT,
the per-pipeline lock, the webhook brace-extraction core, structured logging,
and the shared TTL constants). Everything that differs is preserved verbatim
per pipeline:

  * Provider provisions ONE run VM that runs the container's DEFAULT CMD; it
    reads pipeline.config and snapshots it in the manifest; it resumes the
    Atlas cluster FIRST, then provisions the VM, then writes the reservation
    (reservation references a real VM); it emits a DiscrepancyReport on a
    VM-provision failure; config facts fall back to hardcoded defaults.
  * Medicare provisions ONE Controller VM on the permanent controller NIC that
    runs an explicit python entrypoint (bootstrap.py control_runner.py ...)
    with WORKER_COMPUTE=vm so the Controller provisions its own Worker VMs; it
    carries the --build-fda-drug flag and CH_FDA_ENRICHMENT_MODEL; it RESERVES
    the run FIRST then resumes the cluster (the lease covers the long wake);
    every config fact is required (no defaults).

Each flow is byte-for-byte the behaviour of the runbook it came from. See the
module's delivery note for the specific divergences that are the operator's to
reconcile should a single shared flow ever be wanted.
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

# Ensure required Azure SDK packages are installed in the Automation runtime.
_REQUIRED_PACKAGES = ["azure-identity", "azure-keyvault-secrets", "pymongo", "cryptography"]
for _pkg in _REQUIRED_PACKAGES:
    try:
        __import__(_pkg.replace("-", "_"))
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", _pkg])

# Atlas addresses are SRV records, and the Automation sandbox's own resolver
# does not answer external SRV queries. Point dnspython at public resolvers
# before pymongo is imported (pymongo binds the default resolver at import).
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
    ChatHealthyLoggingService,
    set_mongo_log_identity,
)
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

# ---------------------------------------------------------------------------
# Automation-variable hydration.
# Automation Variables are not in os.environ in the AA sandbox; each has to be
# asked for by name. At IMPORT time we hydrate ONLY the provider trigger's
# original key set, so the provider path resolves exactly the values it did as
# a standalone runbook (vars it never hydrated stay unhydrated and the provider
# constants keep their hardcoded defaults). The medicare path hydrates its own
# (larger) key set lazily in _load_medicare_config() so a provider fire never
# depends on a medicare-only Automation Variable.
# ---------------------------------------------------------------------------
_PROVIDER_HYDRATE_KEYS = (
    "CH_LOG_DB", "CH_LOG_LEVEL", "PIPELINE_SECRET_NAMES",
    "CH_EMBEDDING_MODEL",
    "KEY_VAULT_URI", "AUTOMATION_ENV_PREFIX",
    "AUTOMATION_SUBSCRIPTION_ID", "AUTOMATION_RESOURCE_GROUP",
    "ATLAS_PROJECT_ID", "AZ_VM_ADMIN_SSH_PUBKEY",
    "PIPELINEEDITOR_AZURE_TENANT_ID",
    "PIPELINEEDITOR_AZURE_CLIENT_ID",
    "PIPELINEEDITOR_AZURE_CLIENT_SECRET",
)


def _hydrate(keys) -> None:
    """Copy each named Automation Variable into os.environ if present. Safe to
    call more than once and for keys that do not exist (absent -> skipped)."""
    for _k in keys:
        try:
            import automationassets  # only present in the Automation sandbox
            _v = automationassets.get_automation_variable(_k)
            if _v:
                os.environ[_k] = str(_v)
        except Exception:  # noqa: BLE001
            pass


_hydrate(_PROVIDER_HYDRATE_KEYS)


def _req(name: str) -> str:
    """A required value from the runbook's environment (an Automation Variable
    in AA). Absent or empty is fatal: no default, no fallback. The fact is
    supplied by the deploy, never hardcoded here."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ChatHealthyException(
            mode="config_error",
            message=f"pipeline_runbook: required value {name!r} is absent; "
                    f"it must be supplied by the deploy. No default is applied.",
            component="pipeline_runbook", missing=name)
    return value


# Whose certificate the log handler connects with. Carried as a constant rather
# than imported from pipeline_db, which is not deployed alongside the runbook.
set_mongo_log_identity("pipelineEditor")

# CHLS prerequisites. CH_LOG_DESTINATION drives output to stderr (AA captures
# stderr into the ARM job exception field) and to mongo; CH_SPACE_NAME satisfies
# the Mongo handler prerequisite. ENV_PREFIX is resolved leniently here (the
# provider path tolerates its absence with a 'dev' default); the medicare path
# re-requires it strictly in _load_medicare_config().
os.environ.setdefault("CH_LOG_DESTINATION", "stderr,mongo")
os.environ.setdefault("CH_SPACE_NAME", "runbook")
os.environ.setdefault("ENV_PREFIX", os.environ.get("AUTOMATION_ENV_PREFIX", "dev"))

# All pipeline metadata lives in one database.
PIPELINE_ADMIN_DB = "pipelineAdmin"

# The dispatched pipeline, set by main() before any pipeline flow runs. Drives
# the structured log record's "runbook" field and the per-pipeline helper tags.
# Defaults to the legacy pipeline (provider).
PIPELINE_NAME = "provider"

_HOSTNAME = socket.gethostname()

# Shared trigger constants (identical in both source runbooks).
_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEBUG_LEVEL_DEFAULT = "INFO"
RESERVATION_TTL_HOURS = 24
# A cluster resume must not wait forever: one that never reaches IDLE aborts
# with a fatal report rather than hang the run indefinitely.
RESUME_TIMEOUT_SECONDS = 1200
_PIPELINE_LOCK_TTL_HOURS = 12
ATLAS_ADMIN_BASE = os.environ.get(
    "AUTOMATION_ATLAS_ADMIN_BASE", "https://cloud.mongodb.com/api/atlas/v2"
)
ATLAS_PIPELINE_CLUSTER = os.environ.get(
    "AUTOMATION_ATLAS_PIPELINE_CLUSTER", "ChatHealthyDataPipelines"
)


# ============================================================================
# Shared: managed-identity (pipelineEditor) credential + ARM OAuth token
# ============================================================================
def _editor_credential() -> tuple[str, str, str]:
    """The credential the trigger acts with: pipelineEditor, and nothing else.
    Read from Automation Variables where available, otherwise from os.environ.
    A run either presents pipelineEditor or it does not start."""
    keys = ("PIPELINEEDITOR_AZURE_TENANT_ID",
            "PIPELINEEDITOR_AZURE_CLIENT_ID",
            "PIPELINEEDITOR_AZURE_CLIENT_SECRET")
    values = []
    for k in keys:
        v = ""
        try:
            import automationassets  # only present in the Automation sandbox
            v = str(automationassets.get_automation_variable(k) or "").strip()
        except Exception:  # noqa: BLE001
            pass
        values.append(v or os.environ.get(k, "").strip())
    missing = [k for k, v in zip(keys, values) if not v]
    if missing:
        raise ChatHealthyException(
            mode="identity_credential_absent",
            message="cannot act as pipelineEditor: "
                    + ", ".join(missing) + " absent",
            component="pipeline_runbook",
            missing=",".join(missing),
        )
    return values[0], values[1], values[2]


PIPELINE_EDITOR_TENANT, PIPELINE_EDITOR_CLIENT_ID, PIPELINE_EDITOR_SECRET = (
    _editor_credential())

_TOKEN_CACHE: dict = {}


def _get_token(resource: str, client_id: str | None = None) -> str:
    """An OAuth2 token for `resource`, obtained as pipelineEditor. client_id is
    accepted and ignored: it selected between managed identities on IMDS, and
    there is only one identity now."""
    if resource in _TOKEN_CACHE:
        return _TOKEN_CACHE[resource]
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": PIPELINE_EDITOR_CLIENT_ID,
        "client_secret": PIPELINE_EDITOR_SECRET,
        "scope": resource.rstrip("/") + "/.default",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{PIPELINE_EDITOR_TENANT}/oauth2/v2.0/token",
        data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        _TOKEN_CACHE[resource] = json.loads(r.read().decode("utf-8"))["access_token"]
    return _TOKEN_CACHE[resource]


# ============================================================================
# Shared: structured event logging via ChatHealthyLoggingService (Rule-005).
# ============================================================================
def log(event: str, **fields):
    """Emit one structured event via ChatHealthyLoggingService.LogPipeline. The
    service's MongoLogHandler persists the record to Pipelines.Log_{env}; the
    file/stderr handler mirrors it for job-stream visibility."""
    __ch_log_wrapper__ = True  # noqa: F841  true-caller frame marker
    now = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    record = {
        "ts": now,
        "runbook": PIPELINE_NAME + "_pipeline_runbook",
        "hostname": _HOSTNAME,
        "event": event,
    }
    record.update(fields)
    ChatHealthyLoggingService().LogPipeline("INFO", json.dumps(record, default=str))


# ============================================================================
# Shared: ARM PUT
# ============================================================================
def _put(url: str, body: dict, tok: str, component: str | None = None) -> dict:
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
        except Exception:  # noqa: BLE001
            body_txt = ""
        raise ChatHealthyException(
            mode="runtime_error",
            message=f"ARM PUT {url.rsplit('?', 1)[0]} -> HTTP {e.code}: {body_txt}",
            exception=e,
            component=component or "pipeline_runbook",
        ) from e


# ============================================================================
# Shared: per-pipeline mutual-exclusion lock
# ============================================================================
def _pipeline_lock_id(pipeline_name: str) -> str:
    """Deterministic _id for the same-pipeline mutual-exclusion lock. Same
    pipeline_name -> same _id -> Mongo enforces at-most-one-active via its
    natural _id uniqueness."""
    return f"pipeline_lock:{pipeline_name}"


def _acquire_pipeline_lock(
    mongo, pipeline_name: str, run_id: str, vm_name: str,
) -> dict | None:
    """Atomically acquire the per-pipeline lock. Returns None on success (we
    hold the lock); returns the blocking lock document if another fire already
    holds it (caller MUST abend without touching any shared resource).

    insert_one on a deterministic _id gives the atomic acquire -- two webhooks
    at the same instant -> exactly one insert wins, the other gets
    DuplicateKeyError -> abend. Stale-lock self-heal: an existing lock whose
    expires_at is in the past (crashed VM never released) is deleted and the
    insert retried once, so a crash never wedges the pipeline forever."""
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
                coll.delete_one({"_id": doc["_id"], "run_id": existing.get("run_id")})
                try:
                    coll.insert_one(doc)
                    return None
                except DuplicateKeyError:
                    return coll.find_one({"_id": doc["_id"]})
        return existing


def _release_pipeline_lock(mongo, pipeline_name: str, run_id: str) -> None:
    """Release the per-pipeline lock iff we own it (matched by run_id).
    Idempotent: safe to call in a finally block even if we never acquired."""
    coll = mongo[PIPELINE_ADMIN_DB]["cluster_lifecycle"]
    coll.delete_one({"_id": _pipeline_lock_id(pipeline_name), "run_id": run_id})


# ============================================================================
# Shared: webhook body extraction + pipeline dispatch
# ============================================================================
def _extract_request_body() -> dict:
    """Extract the webhook POST body (RequestBody JSON) from the AA Python-3
    invocation, with no logging. The AA sandbox delivers the webhook envelope
    via sys.argv (unquoted, whitespace-split); reconstruct with a single space
    and extract the RequestBody value via balanced-brace parsing. Returns {}
    when not triggered by a webhook or the body cannot be parsed."""
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
    except Exception:  # noqa: BLE001
        return {}


def _raise_unknown_pipeline(raw) -> None:
    """Raise-only helper: an unknown pipeline_name is rejected rather than
    silently defaulted. Rule-005 keeps the raise off the caller's log call."""
    raise ChatHealthyException(
        mode="value_error",
        message=(
            "pipeline_runbook: unknown pipeline_name "
            f"{raw!r}. Legal values: absent/empty (-> provider), "
            "'provider', 'medicare'. Fire again with a legal value."
        ),
        component="pipeline_runbook",
    )


def _resolve_pipeline_name(raw) -> str:
    """Dispatch rule. Absent / null / empty -> 'provider' (the default; legacy
    callers that send no pipeline_name route here). 'provider'/'medicare' ->
    themselves. Anything else -> logged + rejected (never defaulted)."""
    if raw is None:
        return "provider"
    name = str(raw).strip().lower()
    if name == "":
        return "provider"
    if name in ("provider", "medicare"):
        return name
    log("runbook_reject_unknown_pipeline", pipeline_name=str(raw)[:200])
    _raise_unknown_pipeline(raw)


# ############################################################################
# PROVIDER TRIGGER FLOW (pipeline 0, the default)
# ############################################################################

# ---------- Provider deploy-time invariants (Automation Variables in prod) ---
SUBSCRIPTION_ID = os.environ.get(
    "AUTOMATION_SUBSCRIPTION_ID", "7a17eec1-c477-4c7c-b1c1-d0662ce7a1ee"
)
RESOURCE_GROUP = os.environ.get(
    "AUTOMATION_RESOURCE_GROUP", "rg-chathealthy-pipeline-dev"
)
ENV_PREFIX = os.environ.get("AUTOMATION_ENV_PREFIX", "dev")

# Provider creates a fresh single run VM per run (no ACA job).
VM_LOCATION = os.environ.get("AUTOMATION_VM_LOCATION", "eastus2")
VM_SIZE = os.environ.get("AUTOMATION_VM_SIZE", "Standard_D32s_v6")
VM_SUBNET = os.environ.get("AUTOMATION_VM_SUBNET", "snet-pipeline-compute")
VM_VNET = os.environ.get("AUTOMATION_VM_VNET", "vnet-chathealthy-pipeline-dev")
PIPELINE_SECRET_NAMES = os.environ.get("PIPELINE_SECRET_NAMES", "")
CH_EMBEDDING_MODEL = os.environ.get("CH_EMBEDDING_MODEL", "")
CH_LOG_DB = os.environ.get("CH_LOG_DB", "")
VM_ACR = os.environ.get("AUTOMATION_VM_ACR", "chpipelinedevacr")
VM_IMAGE_REPO = os.environ.get("AUTOMATION_VM_IMAGE_REPO", "pipeline-control")
VM_IMAGE_TAG = os.environ.get("AUTOMATION_VM_IMAGE_TAG", "latest")
ATLAS_PROJECT_ID = os.environ.get("AUTOMATION_ATLAS_PROJECT_ID", "")
KEY_VAULT_URI = os.environ.get(
    "KEY_VAULT_URI", "https://kv-chpipeline-dev.vault.azure.net/"
)

# Provider webhook / cron defaults.
INVOCATION_MODE = os.environ.get("INVOCATION_MODE", "scheduled")
LOAD_MODE_DEFAULT = os.environ.get("LOAD_MODE", "full")
STATE_SCOPE_DEFAULT = os.environ.get("STATE_SCOPE", "ALL")


def _read_pipeline_config(mongo) -> dict:
    """Read chathealthyfrontend.pipeline.config for pipeline_name='provider'.
    Returns config dict or empty dict if the document doesn't exist yet."""
    coll = mongo[PIPELINE_ADMIN_DB]["pipeline.config"]
    doc = coll.find_one({"pipeline_name": PIPELINE_NAME, "env": ENV_PREFIX}) or {}
    return doc


def _reservation_short_id(run_id: str) -> str:
    """Same VM-name suffix _provision_vm computes; used to correlate the
    reservation record with the eventual VM."""
    return run_id.split("-")[-1][:8] if "-" in run_id else run_id[:8]


def _create_reservation(mongo, run_id: str, vm_name: str) -> None:
    """Write the pipeline-run reservation on the front cluster's
    admin.cluster_lifecycle. Controller inherits it and cancels in its
    main.finally; reservation_reaper reaps expiry_at < now otherwise."""
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
            "requester": "pipeline_runbook",
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
    and 409 (delete queued while another op is in flight). Raises on other
    HTTP errors so the caller can log + swallow."""
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


def _report_fatal(run_id: str, exc: ChatHealthyException) -> None:
    """Send the discrepancy report for a failure that happened here. A
    runbook-stage failure never reaches the VM, so this is the only chance
    the run has to report itself, through the same class every other stage
    uses."""
    try:
        from chathealthy_lib.discrepancy_report import DiscrepancyReport
        data_version = (int(os.environ["DATA_VERSION"])
                        if os.environ.get("DATA_VERSION", "").strip().isdigit()
                        else None)
        DiscrepancyReport(
            run_id=run_id,
            env=os.environ.get("ENV_PREFIX", "dev"),
            pipeline_name=PIPELINE_NAME,
            source="PipelineRunbook",
            target_collection=(
                f"PipelinePublicHealthData.Provider_v_{data_version}"),
            total_source_rows=None,
            rows_in_target=0,
            total_rows=None,
            data_version=data_version,
        ).fatal(exc)
        log("runbook_fatal_report_sent", run_id=run_id)
    except Exception as report_exc:  # noqa: BLE001
        log("runbook_fatal_report_failed",
            run_id=run_id, error=str(report_exc)[:500])


def _runbook_error_teardown(mongo, run_id: str, vm_name: str,
                            reservation_created: bool,
                            vm_provisioned: bool) -> None:
    """Called on any error path after either the VM PUT landed or the
    reservation was written. Cancels the reservation and deletes the VM so
    nothing leaks. Idempotent + best-effort."""
    if reservation_created:
        try:
            _cancel_reservation(mongo, run_id)
            log("runbook_teardown_reservation_cancelled", run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            log("runbook_teardown_reservation_cancel_failed",
                run_id=run_id, error=str(exc)[:500])
    if vm_provisioned:
        try:
            _delete_vm_via_arm(vm_name)
            log("runbook_teardown_vm_delete_dispatched",
                run_id=run_id, vm_name=vm_name)
        except Exception as exc:  # noqa: BLE001
            log("runbook_teardown_vm_delete_failed",
                run_id=run_id, vm_name=vm_name, error=str(exc)[:500])


def _raise_missing_data_version(webhook_body) -> None:
    """Raise-only helper: rejects a webhook payload that omits data_version."""
    raise ChatHealthyException(
        mode="value_error",
        message=(
            "pipeline_runbook: webhook payload MUST include "
            "data_version as int >= 1. Fire again with a valid value."
        ),
        webhook_body_head=str(webhook_body)[:400],
    )


def _write_run_manifest(mongo, run_id: str, load_mode: str,
                        state_scope, invocation_mode: str,
                        config: dict) -> None:
    """Fresh manifest in chathealthyfrontend.pipeline.runs."""
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


def _cloud_init_user_data(run_id: str, load_mode: str, state_scope,
                          invocation_mode: str, resume_from_step: str,
                          data_version: int,
                          google_maps_enabled: bool = False,
                          debug_level: str = DEBUG_LEVEL_DEFAULT) -> str:
    """Return the base64-encoded cloud-init user_data blob for the provider
    run VM. The VM pulls the image as pipelineEditor and runs the container's
    DEFAULT CMD (Controller); on clean exit it fires a farewell az vm delete,
    on failure it runs the image once more to report the controller failure
    and leaves the host up for the watchdog."""
    import base64
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
    # Log in as pipelineEditor. The host carries no identity of its own.
    az login --service-principal \\
      --username '{PIPELINE_EDITOR_CLIENT_ID}' \\
      --password '{PIPELINE_EDITOR_SECRET}' \\
      --tenant '{PIPELINE_EDITOR_TENANT}'
    az account set --subscription '{SUBSCRIPTION_ID}'
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
    # Controller's finally block deletes AZURE_VM_NAME through ARM on
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
      -e CH_LOG_LEVEL='{debug_level}' \\
      -e CH_LOG_DB='{CH_LOG_DB}' \\
      -e CH_COMPONENT='provider_pipeline_control' \\
      -e PIPELINE_LOG_ACCOUNT_URL='https://stchpipelinedev.blob.core.windows.net' \\
      -e CH_MONGO_HOST_CHATHEALTHYFRONTEND='chathealthyfrontend-pri.mdwahg.mongodb.net' \\
      -e CH_MONGO_HOST_CHATHEALTHYDATAPIPELINES='chathealthydatapipeline-pri.mdwahg.mongodb.net' \\
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
      -e AZURE_TENANT_ID='{PIPELINE_EDITOR_TENANT}' \\
      -e AZURE_CLIENT_ID='{PIPELINE_EDITOR_CLIENT_ID}' \\
      -e AZURE_CLIENT_SECRET='{PIPELINE_EDITOR_SECRET}' \\
      -e PIPELINEEDITOR_AZURE_TENANT_ID='{PIPELINE_EDITOR_TENANT}' \\
      -e PIPELINEEDITOR_AZURE_CLIENT_ID='{PIPELINE_EDITOR_CLIENT_ID}' \\
      -e PIPELINEEDITOR_AZURE_CLIENT_SECRET='{PIPELINE_EDITOR_SECRET}' \\
      -e PIPELINE_SECRET_NAMES='{PIPELINE_SECRET_NAMES}' \\
      -e CH_EMBEDDING_MODEL='{CH_EMBEDDING_MODEL}' \\
      {image_ref}
    DOCKER_EXIT=$?
    echo "chpipeline: docker run exit=$DOCKER_EXIT $(date -u +%FT%TZ)"
    set -e
    # Farewell VM delete, on success only. A Controller that exited non-zero
    # left the reason in /var/log/chpipeline-cloud-init.log and in the
    # container's own output, and deleting the host destroys both. The
    # watchdog reaps what is left behind, so this trades a VM-hour for the
    # ability to know why a run failed.
    if [ "$DOCKER_EXIT" -eq 0 ]; then
      echo "chpipeline: controller exited clean; firing farewell az vm delete $(date -u +%FT%TZ)"
      az vm delete --resource-group {RESOURCE_GROUP} --name {vm_name_for_farewell} --yes --no-wait || true
    else
      echo "chpipeline: controller exited $DOCKER_EXIT; leaving {vm_name_for_farewell} up so the failure can be read. The watchdog will reap it."
      # A controller that does not come up is a fatal error and has to be
      # reported as one. Everything the failing container knew died with it:
      # --rm removed the container, and this log lives on a host that gets
      # deleted. The same image is run again for the sole purpose of saying
      # what happened, because it already carries the library, the identity
      # certificate and the vault credentials.
      docker run --rm --network host \\
        -e CHATHEALTHY_NODE_IDENTITY='pipeline-control' \\
        -e CH_SPACE_NAME='control' \\
        -e CH_LOG_DESTINATION='stderr,mongo' \\
        -e CH_LOG_LEVEL='{debug_level}' \\
        -e CH_LOG_DB='{CH_LOG_DB}' \\
        -e CH_COMPONENT='provider_pipeline_control' \\
        -e PIPELINE_LOG_ACCOUNT_URL='https://stchpipelinedev.blob.core.windows.net' \\
        -e CH_MONGO_HOST_CHATHEALTHYFRONTEND='chathealthyfrontend-pri.mdwahg.mongodb.net' \\
        -e CH_MONGO_HOST_CHATHEALTHYDATAPIPELINES='chathealthydatapipeline-pri.mdwahg.mongodb.net' \\
        -e RUN_ID='{run_id}' \\
        -e ENV_PREFIX='{ENV_PREFIX}' \\
        -e KEY_VAULT_URI='{KEY_VAULT_URI}' \\
        -e PIPELINEEDITOR_AZURE_TENANT_ID='{PIPELINE_EDITOR_TENANT}' \\
        -e PIPELINEEDITOR_AZURE_CLIENT_ID='{PIPELINE_EDITOR_CLIENT_ID}' \\
        -e PIPELINEEDITOR_AZURE_CLIENT_SECRET='{PIPELINE_EDITOR_SECRET}' \\
        -e PIPELINE_SECRET_NAMES='{PIPELINE_SECRET_NAMES}' \\
        -e CONTROLLER_EXIT_CODE="$DOCKER_EXIT" \\
        --entrypoint python \\
        {image_ref} pipeline/run_lifecycle/bootstrap.py pipeline/run_lifecycle/report_controller_failure.py \\
        || echo "chpipeline: could not report the controller failure $(date -u +%FT%TZ)"
    fi
"""
    return base64.b64encode(yaml_body.encode("utf-8")).decode("ascii")


def _get_ssh_pubkey() -> str:
    """Fetch the admin SSH pubkey from Automation Variables
    (AZ_VM_ADMIN_SSH_PUBKEY). The key exists to satisfy ARM validation; the
    NSG denies inbound SSH."""
    try:
        import automationassets
        return automationassets.get_automation_variable("AZ_VM_ADMIN_SSH_PUBKEY")
    except Exception:  # noqa: BLE001
        return os.environ.get("AZ_VM_ADMIN_SSH_PUBKEY", "")


def _provision_vm(run_id: str, load_mode: str, state_scope,
                  invocation_mode: str, resume_from_step: str = "",
                  data_version: int = 0,
                  google_maps_enabled: bool = False,
                  debug_level: str = DEBUG_LEVEL_DEFAULT) -> dict:
    """PUT a fresh Pipeline Run VM into snet-pipeline-compute. Async by nature:
    ARM returns a provisioning-state URL, not a completed VM. The runbook
    returns immediately after the PUT; cloud-init runs on the VM once it
    boots."""
    tok = _get_token("https://management.azure.com/")
    short_id = run_id.split("-")[-1][:8] if "-" in run_id else run_id[:8]
    vm_name = f"vm-chpipeline-{short_id}"
    subnet_id = (
        f"/subscriptions/{SUBSCRIPTION_ID}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.Network/virtualNetworks/{VM_VNET}"
        f"/subnets/{VM_SUBNET}"
    )
    nic_name = f"{vm_name}-nic"
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

    vm_url = (
        f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
        f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Compute/"
        f"virtualMachines/{vm_name}?api-version=2024-03-01"
    )
    user_data_b64 = _cloud_init_user_data(
        run_id, load_mode, state_scope, invocation_mode, resume_from_step,
        data_version, google_maps_enabled, debug_level,
    )
    vm_body = {
        "location": VM_LOCATION,
        "tags": {
            "pipeline_run_id": run_id,
            "pipeline_name": PIPELINE_NAME,
            "env": ENV_PREFIX,
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
                        "deleteOption": "Delete",
                    },
                }],
            },
            "userData": user_data_b64,
        },
    }
    return _put(vm_url, vm_body, tok)


def _atlas_resume_pipeline_cluster() -> dict:
    """POST Atlas Admin API to resume the pipeline cluster, then POLL until it
    reports stateName=IDLE. Missing credentials or project id RAISE so the
    caller aborts the run visibly instead of marching on with a paused
    cluster."""
    try:
        import automationassets  # only in AA sandbox
        atlas_project_id = automationassets.get_automation_variable("ATLAS_PROJECT_ID")
        atlas_pub = automationassets.get_automation_variable("ATLAS_PIPELINE_PUBLIC_KEY")
        atlas_priv = automationassets.get_automation_variable("ATLAS_PIPELINE_PRIVATE_KEY")
    except Exception:  # noqa: BLE001
        atlas_project_id = ATLAS_PROJECT_ID or os.environ.get("ATLAS_PROJECT_ID", "")
        atlas_pub = os.environ.get("ATLAS_PIPELINE_PUBLIC_KEY", "")
        atlas_priv = os.environ.get("ATLAS_PIPELINE_PRIVATE_KEY", "")
    missing = []
    if not atlas_project_id:
        missing.append("ATLAS_PROJECT_ID")
    if not atlas_pub:
        missing.append("ATLAS_PIPELINE_PUBLIC_KEY")
    if not atlas_priv:
        missing.append("ATLAS_PIPELINE_PRIVATE_KEY")
    if missing:
        raise ChatHealthyException(
            mode="atlas_resume_env_unset",
            message=(
                "Atlas Admin credentials missing from Automation Variables: "
                f"{', '.join(missing)}. Cannot resume pipeline cluster "
                f"{ATLAS_PIPELINE_CLUSTER}; run must abort."
            ),
            component="PipelineRunbook",
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
    r = requests.patch(base, auth=auth, headers=headers,
                       json={"paused": False}, timeout=30)
    r.raise_for_status()
    log("atlas_resume_patch_dispatched", cluster=ATLAS_PIPELINE_CLUSTER)
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


def _wake_mongo_then_provision_vm(
    run_id: str, load_mode: str, state_scope,
    invocation_mode: str, resume_from_step: str = "",
    data_version: int = 0,
    google_maps_enabled: bool = False,
    debug_level: str = DEBUG_LEVEL_DEFAULT,
) -> dict:
    """Resume the Atlas cluster to IDLE, then provision the VM. Sequential: a
    host that boots against a still-REPAIRING cluster dies in its own gate. The
    resume raises if it cannot reach IDLE, so a cluster that will not wake costs
    no VM at all."""
    atlas = _atlas_resume_pipeline_cluster()
    vm = _provision_vm(
        run_id, load_mode, state_scope, invocation_mode, resume_from_step,
        data_version, google_maps_enabled, debug_level,
    )
    return {"vm": vm, "atlas": atlas}


def _parse_webhook_input_provider() -> dict:
    """Provider webhook parse: emit the diagnostic payload-discovery event
    (every candidate source with truncated values) and then return the parsed
    RequestBody via the shared brace-extraction core."""
    candidates: dict = {"argv": [str(a)[:400] for a in sys.argv]}
    for k, v in os.environ.items():
        ku = k.upper()
        if any(t in ku for t in ("WEBHOOK", "AUTOMATION", "TRIGGER", "INPUT", "RUNBOOK", "JOB")):
            candidates[f"env:{k}"] = (v or "")[:400]
    try:
        import select as _select
        r, _, _ = _select.select([sys.stdin], [], [], 0.05)
        if r:
            stdin_data = sys.stdin.read(4000)
            candidates["stdin"] = stdin_data[:400]
    except Exception as _e:  # noqa: BLE001
        candidates["stdin_err"] = str(_e)[:200]
    log("webhook_payload_discovery", **{k: v for k, v in list(candidates.items())[:20]})
    return _extract_request_body()


def _raise_bad_debug_level(raw) -> None:
    """Raise-only helper: rejects a debug_level outside the standard level
    names."""
    raise ChatHealthyException(
        mode="value_error",
        message=(
            "pipeline_runbook: debug_level must be one of "
            + ", ".join(_VALID_LOG_LEVELS)
            + f" (case-insensitive). Got {raw!r}. Fire again with a valid "
            + f"value or omit it for the {DEBUG_LEVEL_DEFAULT} default."
        ),
        component="pipeline_runbook",
    )


def _resolve_debug_level(webhook_body) -> str:
    """Resolve the run's log level from the webhook body. Accepts debug_level
    and debuglevel spellings, upper-cases, validates. Absent -> INFO. A
    present-but-bad value is fatal rather than silently coerced."""
    body = webhook_body or {}
    raw = body.get("debug_level")
    if raw is None:
        raw = body.get("debuglevel")
    if raw is None:
        return DEBUG_LEVEL_DEFAULT
    level = str(raw).strip().upper()
    if level not in _VALID_LOG_LEVELS:
        _raise_bad_debug_level(raw)
    return level


def _raise_illegal_state_scope(raw) -> None:
    """Raise-only helper: state_scope is ["ALL"] alone, or a non-empty list of
    state codes with no "ALL" among them."""
    raise ChatHealthyException(
        mode="value_error",
        message=(
            "pipeline_runbook: state_scope must be either [\"ALL\"] "
            "alone or a non-empty list of state codes with no \"ALL\" among "
            f"them. Got {raw!r}. Fire again with a legal scope."
        ),
        component="pipeline_runbook",
    )


def _resolve_state_scope(raw):
    """Parse the webhook state_scope into ["ALL"] alone, or a non-empty list of
    state codes with no "ALL" among them. Anything else abends here."""
    if isinstance(raw, list):
        scope = raw
    elif isinstance(raw, str):
        parsed = None
        if raw.strip().startswith("["):
            try:
                v = json.loads(raw)
                parsed = v if isinstance(v, list) else None
            except Exception:  # noqa: BLE001
                parsed = None
        scope = parsed if parsed is not None else raw.split(",")
    else:
        scope = ["ALL"]
    scope = [str(s).strip().upper() for s in scope if str(s).strip()]
    if not scope or ("ALL" in scope and len(scope) != 1):
        _raise_illegal_state_scope(raw)
    return scope


def _run_provider() -> int:
    """Provider trigger flow."""
    invocation_mode = INVOCATION_MODE
    run_id = str(uuid.uuid4())
    os.environ["RUN_ID"] = run_id
    webhook_body = _parse_webhook_input_provider()
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
    dv_raw = (webhook_body or {}).get("data_version")
    try:
        data_version = int(dv_raw)
    except (TypeError, ValueError):
        data_version = 0
    if data_version < 1:
        log("runbook_reject_no_data_version", webhook_body=str(webhook_body)[:400])
        _raise_missing_data_version(webhook_body)
    os.environ["DATA_VERSION"] = str(data_version)

    gm_raw = (webhook_body or {}).get("google_maps_enabled")
    if isinstance(gm_raw, bool):
        google_maps_enabled = gm_raw
    else:
        google_maps_enabled = str(gm_raw or "").strip().lower() in ("1", "true", "yes")
    os.environ["GOOGLE_MAPS_ENABLED"] = "1" if google_maps_enabled else "0"

    debug_level = _resolve_debug_level(webhook_body)
    os.environ["CH_LOG_LEVEL"] = debug_level

    log("runbook_start",
        pipeline=PIPELINE_NAME,
        env=ENV_PREFIX,
        invocation_mode=invocation_mode,
        load_mode=load_mode,
        state_scope=state_scope,
        data_version=data_version,
        google_maps_enabled=google_maps_enabled,
        debug_level=debug_level,
        resume_from_step=resume_from_step or None)

    log("run_id_generated", run_id=run_id)

    is_duplicate_abend = False
    runbook_owns_lock_release = False
    vm_name_for_teardown = None
    mongo = None
    try:
        import certifi as _certifi
        _mongo_tls_ca = _certifi.where()
    except ImportError:
        _mongo_tls_ca = None
    try:
        try:
            import pymongo  # noqa: F401  provided by AA Python 3 package
            mongo = ChatHealthyMongoUtilities().getConnection(
                "pipelineEditor", "ChatHealthyFrontEnd"
            )
            mongo.admin.command("ping")
            log("mongo_connected", cluster="chathealthydatapipelines")
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
            runbook_owns_lock_release = True
            vm_name_for_teardown = _vm_name_for_lock
            config = _read_pipeline_config(mongo)
            log("config_read",
                config_keys=list(config.keys()),
                found=bool(config))
            _write_run_manifest(mongo, run_id, load_mode, state_scope,
                                invocation_mode, config)
            log("run_manifest_written", run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            log("mongo_step_failed_abort",
                error_type=type(exc).__name__,
                error_msg=str(exc)[:1500],
                traceback=traceback.format_exc()[-2000:])
            return 1

        runbook_owns_lock_release = True
        reservation_created = False
        vm_provisioned = False
        vm_name_for_teardown = f"vm-chpipeline-{_reservation_short_id(run_id)}"
        try:
            result = _wake_mongo_then_provision_vm(
                run_id, load_mode, state_scope, invocation_mode, resume_from_step,
                data_version, google_maps_enabled, debug_level,
            )
            vm_id = ((result.get("vm") or {}).get("id")) if result.get("vm") else None
            vm_provisioned = True
            runbook_owns_lock_release = False
            log("mongo_idle_then_vm_provisioned",
                run_id=run_id,
                vm_id=vm_id)
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
            except Exception:  # noqa: BLE001
                pass
            log("vm_provision_failed",
                run_id=run_id,
                http_code=exc.code,
                reason=exc.reason,
                body=body)
            _report_fatal(run_id, ChatHealthyException(
                mode="vm_provision_failed",
                message=f"VM provisioning returned HTTP {exc.code}: {exc.reason}",
                component="PipelineRunbook",
                run_id=run_id))
            _runbook_error_teardown(mongo, run_id, vm_name_for_teardown,
                                    reservation_created, vm_provisioned)
            return 1
        except Exception as exc:  # noqa: BLE001
            log("vm_provision_failed",
                run_id=run_id,
                error_type=type(exc).__name__,
                error_msg=str(exc),
                traceback=traceback.format_exc()[-2000:])
            _report_fatal(run_id, ChatHealthyException(
                mode="vm_provision_failed",
                message=f"VM provisioning failed: {type(exc).__name__}: {exc}",
                component="PipelineRunbook",
                run_id=run_id))
            _runbook_error_teardown(mongo, run_id, vm_name_for_teardown,
                                    reservation_created, vm_provisioned)
            return 1

        log("runbook_exit", run_id=run_id)
        return 0
    finally:
        if is_duplicate_abend:
            log("duplicate_abend_teardown_skipped", run_id=run_id)
        elif runbook_owns_lock_release:
            try:
                if mongo is not None:
                    _release_pipeline_lock(mongo, PIPELINE_NAME, run_id)
                    log("pipeline_lock_released_runbook_side",
                        pipeline_name=PIPELINE_NAME, run_id=run_id,
                        reason="runbook_failed_before_vm_handoff")
            except Exception as exc:  # noqa: BLE001
                log("pipeline_lock_release_error",
                    pipeline_name=PIPELINE_NAME, run_id=run_id,
                    error=str(exc)[:400])
        else:
            log("pipeline_lock_ownership_handed_to_vm",
                pipeline_name=PIPELINE_NAME, run_id=run_id,
                vm_name=vm_name_for_teardown)


# ############################################################################
# MEDICARE TRIGGER FLOW (pipeline 1)
# Every config fact is required (no defaults) and is hydrated + resolved lazily
# so a provider fire never depends on a medicare-only Automation Variable.
# ############################################################################

_MEDICARE_HYDRATE_KEYS = (
    "CH_LOG_DB", "CH_LOG_LEVEL", "PIPELINE_SECRET_NAMES", "CH_EMBEDDING_MODEL",
    "CH_FDA_ENRICHMENT_MODEL",
    "KEY_VAULT_URI", "AUTOMATION_ENV_PREFIX",
    "AUTOMATION_SUBSCRIPTION_ID", "AUTOMATION_RESOURCE_GROUP",
    "ATLAS_PROJECT_ID", "ATLAS_PIPELINE_PUBLIC_KEY", "ATLAS_PIPELINE_PRIVATE_KEY",
    "PIPELINE_CLUSTER", "AZ_VM_ADMIN_SSH_PUBKEY",
    "CONTROLLER_VM_SIZE", "WORKER_VM_SIZE",
    "AUTOMATION_VM_LOCATION", "AUTOMATION_VM_SUBNET", "AUTOMATION_VM_VNET",
    "AUTOMATION_CONTROLLER_NIC",
    "AUTOMATION_VM_ACR", "AUTOMATION_VM_IMAGE_REPO", "AUTOMATION_VM_IMAGE_TAG",
    "CH_MONGO_HOST_CHATHEALTHYFRONTEND", "CH_MONGO_HOST_CHATHEALTHYDATAPIPELINES",
    "PIPELINE_LOG_ACCOUNT_URL",
    "PIPELINEEDITOR_AZURE_TENANT_ID", "PIPELINEEDITOR_AZURE_CLIENT_ID",
    "PIPELINEEDITOR_AZURE_CLIENT_SECRET",
    "PIPELINECONTROLLER_AZURE_TENANT_ID", "PIPELINECONTROLLER_AZURE_CLIENT_ID",
    "PIPELINECONTROLLER_AZURE_CLIENT_SECRET",
)

# Every resource the runbook itself must have present to run -- the full
# hydrate set minus CH_LOG_LEVEL, the one optional key (it has a safe default).
# _check_resources() reports which of these are absent, all at once.
_MEDICARE_REQUIRED_RESOURCES = tuple(
    k for k in _MEDICARE_HYDRATE_KEYS if k != "CH_LOG_LEVEL")

# Medicare config globals, populated by _load_medicare_config(). Declared here
# so the medicare helpers resolve them as module names at call time.
CONTROLLER_VM_SIZE = ""
WORKER_VM_SIZE = ""
CONTROLLER_NIC = ""
CH_FDA_ENRICHMENT_MODEL = ""
PIPELINE_LOG_ACCOUNT_URL = ""
MONGO_HOST_FRONTEND = ""
MONGO_HOST_PIPELINE = ""
PIPELINE_CLUSTER = ""
ATLAS_PIPELINE_PUBLIC_KEY = ""
ATLAS_PIPELINE_PRIVATE_KEY = ""


def _load_medicare_config() -> None:
    """Hydrate and resolve every medicare deploy fact. Required: absent is
    fatal. Called at the top of the medicare flow so a provider fire never
    touches a medicare-only Automation Variable. Overwrites the shared-name
    globals (SUBSCRIPTION_ID, RESOURCE_GROUP, ENV_PREFIX, VM_*, KEY_VAULT_URI,
    PIPELINE_SECRET_NAMES, CH_EMBEDDING_MODEL, CH_LOG_DB, ATLAS_PROJECT_ID)
    with their _req values; the provider flow never runs when this does."""
    global SUBSCRIPTION_ID, RESOURCE_GROUP, ENV_PREFIX
    global CONTROLLER_VM_SIZE, WORKER_VM_SIZE, VM_LOCATION, VM_SUBNET, VM_VNET
    global CONTROLLER_NIC, VM_ACR, VM_IMAGE_REPO, VM_IMAGE_TAG
    global KEY_VAULT_URI, PIPELINE_SECRET_NAMES, CH_EMBEDDING_MODEL
    global CH_FDA_ENRICHMENT_MODEL, CH_LOG_DB, PIPELINE_LOG_ACCOUNT_URL
    global MONGO_HOST_FRONTEND, MONGO_HOST_PIPELINE
    global PIPELINE_CLUSTER, ATLAS_PROJECT_ID
    global ATLAS_PIPELINE_PUBLIC_KEY, ATLAS_PIPELINE_PRIVATE_KEY
    _hydrate(_MEDICARE_HYDRATE_KEYS)
    os.environ["ENV_PREFIX"] = _req("AUTOMATION_ENV_PREFIX")
    ENV_PREFIX = os.environ["ENV_PREFIX"]
    os.environ.setdefault("CH_COMPONENT", "pipeline_runbook")
    SUBSCRIPTION_ID = _req("AUTOMATION_SUBSCRIPTION_ID")
    RESOURCE_GROUP = _req("AUTOMATION_RESOURCE_GROUP")
    CONTROLLER_VM_SIZE = _req("CONTROLLER_VM_SIZE")
    WORKER_VM_SIZE = _req("WORKER_VM_SIZE")
    VM_LOCATION = _req("AUTOMATION_VM_LOCATION")
    VM_SUBNET = _req("AUTOMATION_VM_SUBNET")
    VM_VNET = _req("AUTOMATION_VM_VNET")
    CONTROLLER_NIC = _req("AUTOMATION_CONTROLLER_NIC")
    VM_ACR = _req("AUTOMATION_VM_ACR")
    VM_IMAGE_REPO = _req("AUTOMATION_VM_IMAGE_REPO")
    VM_IMAGE_TAG = _req("AUTOMATION_VM_IMAGE_TAG")
    KEY_VAULT_URI = _req("KEY_VAULT_URI")
    PIPELINE_SECRET_NAMES = _req("PIPELINE_SECRET_NAMES")
    CH_EMBEDDING_MODEL = _req("CH_EMBEDDING_MODEL")
    CH_FDA_ENRICHMENT_MODEL = _req("CH_FDA_ENRICHMENT_MODEL")
    CH_LOG_DB = _req("CH_LOG_DB")
    PIPELINE_LOG_ACCOUNT_URL = _req("PIPELINE_LOG_ACCOUNT_URL")
    MONGO_HOST_FRONTEND = _req("CH_MONGO_HOST_CHATHEALTHYFRONTEND")
    MONGO_HOST_PIPELINE = _req("CH_MONGO_HOST_CHATHEALTHYDATAPIPELINES")
    PIPELINE_CLUSTER = _req("PIPELINE_CLUSTER")
    ATLAS_PROJECT_ID = _req("ATLAS_PROJECT_ID")
    ATLAS_PIPELINE_PUBLIC_KEY = _req("ATLAS_PIPELINE_PUBLIC_KEY")
    ATLAS_PIPELINE_PRIVATE_KEY = _req("ATLAS_PIPELINE_PRIVATE_KEY")


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
            component="pipeline_runbook", missing=",".join(missing))
    return vals[0], vals[1], vals[2]


def _short(run_id: str) -> str:
    return run_id.split("-")[-1][:8] if "-" in run_id else run_id[:8]


def _write_run_manifest_med(mongo, run_id: str, data_version: int,
                            build_fda_drug: bool, invocation_mode: str,
                            states: list[str]) -> None:
    mongo[PIPELINE_ADMIN_DB]["pipeline.runs"].insert_one({
        "run_id": run_id, "pipeline_name": PIPELINE_NAME, "env": ENV_PREFIX,
        "status": "running", "started_at": datetime.datetime.utcnow(),
        "invocation_mode": invocation_mode, "data_version": data_version,
        "build_fda_drug": build_fda_drug, "states": states})


def _cancel_reservation_med(mongo, run_id: str) -> None:
    mongo[PIPELINE_ADMIN_DB]["cluster_lifecycle"].delete_one({"_id": run_id})


def _get_ssh_pubkey_med() -> str:
    return _req("AZ_VM_ADMIN_SSH_PUBKEY")


def _controller_cloud_init(run_id: str, data_version: int,
                           build_fda_drug: bool, vm_name: str,
                           debug_level: str, states: list[str],
                           provider_collection: str) -> str:
    """Base64 cloud-init for the Controller VM: pull the image, run the Medicare
    control_runner with WORKER_COMPUTE=vm so the orchestrator provisions Worker
    VMs, then self-delete. The Controller presents pipelineController for ARM
    (Worker-VM creation) and its own Mongo cert auth; pipelineEditor credentials
    are handed down for the Worker VMs."""
    import base64
    ct, cc, cs = _controller_credential()
    image = f"{VM_ACR}.azurecr.io/{VM_IMAGE_REPO}:{VM_IMAGE_TAG}"
    bim = "--build-fda-drug" if build_fda_drug else ""
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
    az login --service-principal --username '{PIPELINE_EDITOR_CLIENT_ID}' --password '{PIPELINE_EDITOR_SECRET}' --tenant '{PIPELINE_EDITOR_TENANT}'
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
      -e AZURE_TENANT_ID='{PIPELINE_EDITOR_TENANT}' -e AZURE_CLIENT_ID='{PIPELINE_EDITOR_CLIENT_ID}' -e AZURE_CLIENT_SECRET='{PIPELINE_EDITOR_SECRET}' \\
      -e WORKER_COMPUTE='vm' \\
      -e AZURE_SUBSCRIPTION_ID='{SUBSCRIPTION_ID}' -e AZURE_RESOURCE_GROUP='{RESOURCE_GROUP}' \\
      -e AUTOMATION_VM_LOCATION='{VM_LOCATION}' -e AUTOMATION_VM_VNET='{VM_VNET}' \\
      -e AUTOMATION_VM_SUBNET='{VM_SUBNET}' -e AUTOMATION_VM_ACR='{VM_ACR}' \\
      -e AUTOMATION_VM_IMAGE_REPO='{VM_IMAGE_REPO}' -e AUTOMATION_VM_IMAGE_TAG='{VM_IMAGE_TAG}' \\
      -e WORKER_VM_SIZE='{WORKER_VM_SIZE}' \\
      -e AZ_VM_ADMIN_SSH_PUBKEY='{_get_ssh_pubkey_med()}' \\
      -e KEY_VAULT_URI='{KEY_VAULT_URI}' -e PIPELINE_SECRET_NAMES='{PIPELINE_SECRET_NAMES}' \\
      -e CH_EMBEDDING_MODEL='{CH_EMBEDDING_MODEL}' \\
      -e CH_FDA_ENRICHMENT_MODEL='{CH_FDA_ENRICHMENT_MODEL}' \\
      -e PIPELINECONTROLLER_AZURE_TENANT_ID='{ct}' \\
      -e PIPELINECONTROLLER_AZURE_CLIENT_ID='{cc}' \\
      -e PIPELINECONTROLLER_AZURE_CLIENT_SECRET='{cs}' \\
      -e PIPELINEEDITOR_AZURE_TENANT_ID='{PIPELINE_EDITOR_TENANT}' \\
      -e PIPELINEEDITOR_AZURE_CLIENT_ID='{PIPELINE_EDITOR_CLIENT_ID}' \\
      -e PIPELINEEDITOR_AZURE_CLIENT_SECRET='{PIPELINE_EDITOR_SECRET}' \\
      --entrypoint python {image} \\
      pipeline/run_lifecycle/bootstrap.py \\
      pipeline/MedicareProviderEvaluationDataPipeline/control_runner.py \\
      --run-id {run_id} --env-prefix {ENV_PREFIX} --data-version {data_version} --states {",".join(states)} --provider-collection '{provider_collection}' {bim}
    rc=$?
    echo "chcontrol: controller exit=$rc $(date -u +%FT%TZ)"
    set -e
    az login --service-principal --username '{cc}' --password '{cs}' --tenant '{ct}' || true
    if [ "$rc" -eq 0 ]; then
      az vm delete --resource-group {RESOURCE_GROUP} --name {vm_name} --yes --no-wait || true
    else
      echo "chcontrol: controller FAILED rc=$rc -- VM kept so the crash log survives"
    fi
"""
    return base64.b64encode(yaml_body.encode("utf-8")).decode("ascii")


def _provision_controller_vm(run_id: str, data_version: int,
                             build_fda_drug: bool, debug_level: str,
                             states: list[str],
                             provider_collection: str) -> dict:
    tok = _get_token("https://management.azure.com/")
    vm_name = f"vm-chcontrol-{_short(run_id)}"
    tags = {"pipeline_run_id": run_id, "pipeline_name": PIPELINE_NAME, "env": ENV_PREFIX}
    user_data = _controller_cloud_init(run_id, data_version, build_fda_drug,
                                       vm_name, debug_level, states,
                                       provider_collection)
    vm_url = (f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
              f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Compute/"
              f"virtualMachines/{vm_name}?api-version=2024-03-01")
    # The controller attaches to the permanent VNET node -- the deploy-created
    # controller NIC that carries the static public IP. deleteOption Detach
    # leaves the NIC and its IP intact when the ephemeral VM is torn down, so
    # the next run's controller reattaches the same address.
    nic_id = (f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
              f"/providers/Microsoft.Network/networkInterfaces/{CONTROLLER_NIC}")
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
                                                  "keyData": _get_ssh_pubkey_med()}]},
                          "provisionVMAgent": True},
                      "customData": user_data},
        "networkProfile": {"networkInterfaces": [{"id": nic_id,
            "properties": {"primary": True, "deleteOption": "Detach"}}]},
        "userData": user_data}}, tok, component="pipeline_runbook")
    return {"vm_name": vm_name}


def _raise_missing_data_version_med(body) -> None:
    raise ChatHealthyException(
        mode="value_error",
        message="pipeline_runbook: webhook payload MUST include data_version "
                "as int >= 1. Fire again with a valid value.",
        component="pipeline_runbook", webhook_body_head=str(body)[:400])


def _raise_illegal_states(raw) -> None:
    raise ChatHealthyException(
        mode="value_error",
        message="pipeline_runbook: states must be either [\"ALL\"] alone "
                "or a non-empty list of state codes with no \"ALL\" among them. "
                f"Got {raw!r}. Fire again with a legal scope.",
        component="pipeline_runbook")


def _resolve_states(raw) -> list[str]:
    """Parse the webhook states into the provider-identical scope: ["ALL"] alone,
    or a non-empty list of state codes with no "ALL" among them. Absent ->
    ["ALL"]. Illegal shapes abend here, at the parse."""
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


def _reserve_and_resume_cluster(mongo, run_id: str, vm_name: str,
                                ttl_hours: int) -> None:
    """The one and only place that manages the pipeline data cluster for a run.

    1. Reserve the run FIRST -- before the (tens-of-minutes) resume -- so the
       run holds a live lease for the whole wake and the reservation reaper does
       not reap it as controller-never-started.
    2. Resume the always-paused cluster and poll until it reports IDLE.

    Missing Atlas credentials RAISE -- a run that cannot wake its data cluster
    aborts visibly rather than march on."""
    now = datetime.datetime.utcnow()
    mongo[PIPELINE_ADMIN_DB]["cluster_lifecycle"].replace_one(
        {"_id": run_id},
        {"_id": run_id, "run_id": run_id, "vm_name": vm_name,
         "cluster_name": ATLAS_PIPELINE_CLUSTER, "requester": "pipeline_runbook",
         "start_time": now,
         "expiry_at": now + datetime.timedelta(hours=ttl_hours),
         "reservation_class": "pipeline_run", "pipeline_name": PIPELINE_NAME,
         "status": "active"},
        upsert=True)
    log("reservation_created", run_id=run_id, vm_name=vm_name, ttl_hours=ttl_hours)

    missing = [n for n, v in (("ATLAS_PROJECT_ID", ATLAS_PROJECT_ID),
                              ("ATLAS_PIPELINE_PUBLIC_KEY", ATLAS_PIPELINE_PUBLIC_KEY),
                              ("ATLAS_PIPELINE_PRIVATE_KEY", ATLAS_PIPELINE_PRIVATE_KEY),
                              ("PIPELINE_CLUSTER", PIPELINE_CLUSTER)) if not v]
    if missing:
        raise ChatHealthyException(
            mode="atlas_resume_env_unset",
            message="Atlas Admin credentials missing: " + ", ".join(missing)
                    + f"; cannot wake cluster {PIPELINE_CLUSTER or '<unset>'}.",
            component="pipeline_runbook", missing=",".join(missing))
    import requests  # noqa: PLC0415
    from requests.auth import HTTPDigestAuth  # noqa: PLC0415
    base = f"{ATLAS_ADMIN_BASE}/groups/{ATLAS_PROJECT_ID}/clusters/{PIPELINE_CLUSTER}"
    headers = {"Content-Type": "application/vnd.atlas.2024-08-05+json",
               "Accept": "application/vnd.atlas.2024-08-05+json"}
    auth = HTTPDigestAuth(ATLAS_PIPELINE_PUBLIC_KEY, ATLAS_PIPELINE_PRIVATE_KEY)
    r = requests.patch(base, auth=auth, headers=headers,
                       json={"paused": False}, timeout=30)
    r.raise_for_status()
    log("atlas_resume_patch_dispatched", cluster=PIPELINE_CLUSTER)
    import time as _time  # noqa: PLC0415
    deadline = _time.monotonic() + RESUME_TIMEOUT_SECONDS
    while True:
        g = requests.get(base, auth=auth, headers=headers, timeout=30)
        g.raise_for_status()
        state = (g.json() or {}).get("stateName", "")
        if state == "IDLE":
            log("atlas_resume_cluster_idle", cluster=PIPELINE_CLUSTER)
            return
        if _time.monotonic() >= deadline:
            raise ChatHealthyException(
                mode="atlas_resume_timeout",
                message=f"cluster {PIPELINE_CLUSTER} did not reach IDLE within "
                        f"{RESUME_TIMEOUT_SECONDS // 60} minutes (last state "
                        f"{state or 'unknown'}); aborting rather than waiting "
                        f"indefinitely.",
                component="pipeline_runbook")
        log("atlas_resume_waiting", cluster=PIPELINE_CLUSTER, state=state or "unknown")
        _time.sleep(20)


def _require_mongo_logging_or_die():
    """Prove the Mongo the logger writes to is reachable before doing anything.

    A run we cannot log is a run we cannot observe, which is worse than no run.
    This is the first thing the flow does; on failure it writes to stderr (the
    one channel left, which Azure Automation captures into the job's exception
    field) and re-raises, so nothing else is attempted. Returns the live client
    so the run reuses this one connection rather than opening a second."""
    try:
        client = ChatHealthyMongoUtilities().getConnection(
            "pipelineEditor", "ChatHealthyFrontEnd")
        client.admin.command("ping")
        return client
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            "FATAL pipeline_runbook: the Mongo logging target "
            "(pipelineEditor@ChatHealthyFrontEnd) is unreachable: "
            f"{exc}. Aborting before any work -- a run that cannot be logged "
            "cannot be observed.\n")
        sys.stderr.flush()
        raise


def _check_resources() -> list:
    """Every resource the runbook needs, checked for presence in one pass.

    Returns the list of missing resource names (empty when all present) so the
    caller can abend with the COMPLETE list rather than failing on the first
    absent one. Scope is the runbook's OWN resources; what the Controller needs
    at run time is the Controller's to check, not this runbook's."""
    _hydrate(_MEDICARE_HYDRATE_KEYS)
    return [name for name in _MEDICARE_REQUIRED_RESOURCES
            if not os.environ.get(name, "").strip()]


def _emit_medicare_fatal_report(run_id: str, data_version: int,
                                exc: Exception) -> None:
    """Send the fatal discrepancy report for a medicare abend. Never raises:
    a failure to report must not mask the failure being reported."""
    try:
        from chathealthy_lib.discrepancy_report import DiscrepancyReport  # noqa: PLC0415
        target = ("PipelinePublicHealthData.ProviderMedicare_v_%d" % data_version
                  if data_version else "PipelinePublicHealthData.ProviderMedicare")
        DiscrepancyReport(
            run_id=run_id,
            env=os.environ.get("ENV_PREFIX", "dev"),
            pipeline_name=PIPELINE_NAME,
            source="PipelineRunbook",
            target_collection=target,
            total_source_rows=None,
            rows_in_target=0,
            total_rows=None,
            data_version=data_version or None,
        ).fatal(exc)
        log("runbook_fatal_report_sent", run_id=run_id)
    except Exception as report_exc:  # noqa: BLE001
        log("runbook_fatal_report_failed", run_id=run_id,
            error=str(report_exc)[:400])


def _run_medicare() -> int:
    """Medicare trigger flow.

    Fail fast and visibly rather than hang:
      1. Prove Mongo logging works -- die on stderr if not.
      2. Verify every resource the runbook needs is present -- abend with the
         full list of what is missing if not.
      3. Run (lock, manifest, resume the cluster, provision the Controller).
    Any abend in steps 2-3 sends a fatal discrepancy report."""
    run_id = str(uuid.uuid4())
    os.environ["RUN_ID"] = run_id
    data_version = 0
    runbook_owns_lock = False
    reservation_created = False
    vm_name = f"vm-chcontrol-{_short(run_id)}"
    # Step 1 -- logging first, outside the try: if we cannot log, we cannot even
    # report a fatal, so stderr-and-die is the only honest option.
    mongo = _require_mongo_logging_or_die()
    try:
        # Step 2 -- every resource the runbook needs, up front, as one list.
        missing = _check_resources()
        if missing:
            raise ChatHealthyException(
                mode="missing_resources",
                message=("pipeline_runbook cannot start: %d required "
                         "resource(s) absent: %s"
                         % (len(missing), ", ".join(missing))),
                component="pipeline_runbook", missing=",".join(missing))
        _load_medicare_config()

        # Step 3 -- run.
        body = _extract_request_body()
        invocation_mode = body.get("invocation_mode", "webhook") if body else "scheduled"
        try:
            data_version = int((body or {}).get("data_version"))
        except (TypeError, ValueError):
            data_version = 0
        if data_version < 1:
            log("runbook_reject_no_data_version", body=str(body)[:400])
            _raise_missing_data_version_med(body)
        bim_raw = (body or {}).get("build_fda_drug")
        build_fda_drug = (bim_raw is True
                          or str(bim_raw or "").strip().lower() in ("1", "true", "yes"))
        debug_level = str((body or {}).get("debug_level")
                          or DEBUG_LEVEL_DEFAULT).strip().upper()
        if debug_level not in _VALID_LOG_LEVELS:
            debug_level = DEBUG_LEVEL_DEFAULT
        states_raw = (body or {}).get("states")
        if states_raw is None:
            states_raw = (body or {}).get("state_scope")
        states = _resolve_states(states_raw)
        # The provider source is named explicitly (cluster.db.collection), not
        # derived from data_version. Format-check it at entry: an absent or
        # mangled providerCollection abends now, before any resource is touched.
        provider_collection = ((body or {}).get("providerCollection")
                               or (body or {}).get("provider_collection"))
        from pipeline.MedicareProviderEvaluationDataPipeline.provider_source import (  # noqa: E501
            parse_provider_source, confirm_provider_source_exists)
        parse_provider_source(provider_collection)

        log("runbook_start", pipeline=PIPELINE_NAME, env=ENV_PREFIX,
            invocation_mode=invocation_mode, data_version=data_version,
            build_fda_drug=build_fda_drug, debug_level=debug_level,
            states=states)

        blocking = _acquire_pipeline_lock(mongo, PIPELINE_NAME, run_id, vm_name)
        if blocking is not None:
            log("pipeline_already_running_abend", attempted_run_id=run_id,
                live_run_id=blocking.get("run_id"))
            return 1
        runbook_owns_lock = True
        _write_run_manifest_med(mongo, run_id, data_version, build_fda_drug,
                                invocation_mode, states)
        log("run_manifest_written", run_id=run_id)

        _reserve_and_resume_cluster(mongo, run_id, vm_name, RESERVATION_TTL_HOURS)
        reservation_created = True

        # Cluster is awake: confirm the named provider source exists before any
        # compute is spent. A missing collection abends here, at the start of
        # the run, not after the Controller has worked for an hour.
        confirm_provider_source_exists(
            ChatHealthyMongoUtilities().getConnection(
                "pipelineEditor", "ChatHealthyDataPipelines"),
            provider_collection)

        _provision_controller_vm(run_id, data_version, build_fda_drug,
                                 debug_level, states, provider_collection)
        runbook_owns_lock = False  # Controller owns lock release from here
        log("controller_vm_provisioned", run_id=run_id, vm_name=vm_name)
        log("runbook_exit", run_id=run_id)
        return 0
    except Exception as exc:  # noqa: BLE001
        log("runbook_failed", run_id=run_id, error_type=type(exc).__name__,
            error=str(exc)[:1200], traceback=traceback.format_exc()[-1500:])
        _emit_medicare_fatal_report(run_id, data_version, exc)
        if mongo is not None:
            if reservation_created:
                try:
                    _cancel_reservation_med(mongo, run_id)
                except Exception:  # noqa: BLE001
                    pass
            if runbook_owns_lock:
                try:
                    _release_pipeline_lock(mongo, PIPELINE_NAME, run_id)
                except Exception:  # noqa: BLE001
                    pass
        return 1


# ============================================================================
# Gateway dispatch
# ============================================================================
def main() -> int:
    global PIPELINE_NAME
    body = _extract_request_body()
    name = _resolve_pipeline_name((body or {}).get("pipeline_name"))
    PIPELINE_NAME = name
    os.environ.setdefault("CH_COMPONENT", name + "_pipeline_runbook")
    if name == "medicare":
        return _run_medicare()
    return _run_provider()


if __name__ == "__main__":
    sys.exit(main())
