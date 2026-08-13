"""watchdog_runbook.py -- v32 §3.1.2.

Runs in Azure Automation as a Python 3 runbook on a 30-minute cron. Its
job is to reap orphaned Pipeline Run VMs that survived a Controller-death
event and would otherwise accrue idle cost, WITHOUT killing runs that
are legitimately in-flight.

Detection state machine (per LLD v32 §3.1.2). For each VM tagged
`pipeline_run_id`, the Watchdog performs the following ordered checks --
a VM is deleted ONLY when the checks confirm it is safe to reap:

  1. Reservation check.
     Read `chathealthyfrontend.pipeline.cluster_reservations` for the
     matching `run_id`. If a reservation exists AND has not expired
     (created_at + expected_duration_minutes + 30-min grace < now), the
     run is still legitimately live -> SKIP this VM entirely. This is the
     primary safeguard against killing our own jobs.

  2. Run manifest terminal-status check.
     Read `chathealthyfrontend.pipeline.runs` for the same `run_id`. If
     status  in  {success, failed, aborted}, the run is done -> DELETE the
     VM (cleanup, not a kill).

  3. Controller heartbeat + Azure PowerState check.
     If the manifest is `status  in  {pending_vm_provision, running}` and
     `controller_heartbeat_at` is older than 15 minutes, additionally
     query `az vm get-instance-view` for the VM. If PowerState is
     `deallocated` or `stopped` -> mark manifest `status=failed,
     abort_reason=controller_heartbeat_lost` and DELETE. If PowerState
     is `running` -> mark manifest `status=failed,
     abort_reason=controller_process_dead` and DELETE.

  4. Missing manifest.
     If no run manifest exists for the VM's `pipeline_run_id` tag, the
     VM was orphaned before manifest write completed -> DELETE after a
     30-minute grace to allow slow manifest writes.

Every event is logged to the pipeline-logs blob container in our
datalake. Instrumentation is the Watchdog's sole visibility surface.
"""
from __future__ import annotations
# Atlas addresses are SRV records, and the Automation sandbox's own resolver
# does not answer external SRV queries -- every connection died on
# "All nameservers failed to answer the query _mongodb._tcp.<host> IN SRV".
# Point dnspython at public resolvers instead. This MUST run before pymongo is
# imported, which the chathealthy_lib import below does, because pymongo binds
# the default resolver at import time.
try:
    import dns.resolver  # type: ignore[import-not-found]
    _r = dns.resolver.Resolver(configure=False)
    _r.nameservers = ["8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1"]
    _r.timeout = 5
    _r.lifetime = 10
    dns.resolver.default_resolver = _r
except ImportError:
    pass

from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

import datetime
import json
import os
import socket
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request

# Breadcrumbs to stderr from the first line. Everything below can fail
# before the Mongo handler exists, and when it does stderr is the only
# surface that survives -- it lands in the Automation job record. The
# watchdog spent the afternoon failing on every tick with nothing in the
# log to say so.
sys.stderr.write("watchdog: module import begin\n")

# All pipeline metadata lives in one database. This runbook ships to Azure
# Automation standalone, so it carries the constant rather than importing
# pipeline_db, which is not deployed alongside it.
PIPELINE_ADMIN_DB = "pipelineAdmin"

# Automation Variables are not in os.environ in the sandbox; each is asked
# for by name. CH_LOG_DB in particular, because the log handler refuses to
# start without it, and the identity's credential, because the library
# resolves it from the environment by identity name.
for _k in ("CH_LOG_DB", "CH_LOG_LEVEL", "AUTOMATION_ENV_PREFIX",
           "AUTOMATION_SUBSCRIPTION_ID", "AUTOMATION_RESOURCE_GROUP",
           "KEY_VAULT_URI",
           "PIPELINEEDITOR_AZURE_TENANT_ID",
           "PIPELINEEDITOR_AZURE_CLIENT_ID",
           "PIPELINEEDITOR_AZURE_CLIENT_SECRET"):
    try:
        import automationassets  # only present in the Automation sandbox
        _v = automationassets.get_automation_variable(_k)
        if _v:
            os.environ[_k] = str(_v)
    except Exception:
        pass

# CHLS env prerequisites for AA sandbox visibility. Set before any log()
# call so log output flows to stderr (captured by AA). Mongo destination
# is wired via bootstrap_aa_mongo_logging inside main() BEFORE the first
# log() call, so runbook events land in {env}_Pipelines.Log_{env}.
os.environ.setdefault("CH_SPACE_NAME", "watchdog")
os.environ.setdefault("ENV_PREFIX",
                      os.environ.get("AUTOMATION_ENV_PREFIX", "dev"))
os.environ.setdefault("CH_COMPONENT", "watchdog")
sys.stderr.write(
    f"watchdog: env hydrated; CH_LOG_DB={os.environ.get('CH_LOG_DB', '<unset>')!r} "
    f"ENV_PREFIX={os.environ.get('ENV_PREFIX', '<unset>')!r} "
    f"identity_secret_present={bool(os.environ.get('PIPELINEEDITOR_AZURE_CLIENT_SECRET'))}\n"
)


SUBSCRIPTION_ID = os.environ.get(
    "AUTOMATION_SUBSCRIPTION_ID", "7a17eec1-c477-4c7c-b1c1-d0662ce7a1ee"
)
RESOURCE_GROUP = os.environ.get(
    "AUTOMATION_RESOURCE_GROUP", "rg-chathealthy-pipeline-dev"
)
# Hydrate deploy-pushed Automation Variables into os.environ so this
# runbook picks up the per-env value for KEY_VAULT_URI etc. instead of
# falling back to a dev-hardcoded default. deploy_chain pushes these
# from the secrets block on the DeploymentTargetRecord.
try:
    import automationassets as _aa
    for _k in (
        "KEY_VAULT_URI",
        "AUTOMATION_ENV_PREFIX",
        "PIPELINE_LOG_ACCOUNT_URL",
        "PIPELINE_LOG_CONTAINER",
        "AUTOMATION_SUBSCRIPTION_ID",
        "AUTOMATION_RESOURCE_GROUP",
    ):
        try:
            os.environ[_k] = str(_aa.get_automation_variable(_k))
        except Exception:
            pass
except ImportError:
    pass

ENV_PREFIX = os.environ.get("AUTOMATION_ENV_PREFIX", "dev")
PIPELINE_NAME = "provider"
LOG_ACCOUNT_URL = os.environ.get(
    "PIPELINE_LOG_ACCOUNT_URL", "https://stchpipelinedev.blob.core.windows.net"
)
LOG_CONTAINER = os.environ.get("PIPELINE_LOG_CONTAINER", "pipeline-logs")
KEY_VAULT_URI = os.environ.get(
    "KEY_VAULT_URI", "https://kv-chpipeline-dev.vault.azure.net/"
)

HEARTBEAT_STALE_MINUTES = int(os.environ.get(
    "WATCHDOG_HEARTBEAT_STALE_MINUTES", "15"
))
MISSING_MANIFEST_GRACE_MINUTES = int(os.environ.get(
    "WATCHDOG_MISSING_MANIFEST_GRACE_MINUTES", "30"
))
RESERVATION_EXTRA_GRACE_MINUTES = int(os.environ.get(
    "WATCHDOG_RESERVATION_EXTRA_GRACE_MINUTES", "30"
))

_HOSTNAME = socket.gethostname()


def _load_user_mi_client_id() -> str:
    try:
        import automationassets
        val = automationassets.get_automation_variable("AZURE_CLIENT_ID")
        if val:
            return str(val).strip()
    except Exception:
        pass
    return os.environ.get("AZURE_CLIENT_ID", "").strip()


AZURE_CLIENT_ID = _load_user_mi_client_id()


def _get_token(resource: str, client_id: str | None = None) -> str:
    """An ARM token for pipelineEditor.

    The Automation Account carried a user-assigned managed identity and this
    asked IMDS for every token. That identity was retired when the pipeline
    moved to the pipelineEditor service principal, so every request failed and
    the watchdog reaped nothing -- which is why a finished run's 32-core host
    survived long enough to consume the regional quota.

    The service principal is hydrated into the environment above; the managed
    identity path is kept for any host that still has one.
    """
    tenant = os.environ.get("PIPELINEEDITOR_AZURE_TENANT_ID", "").strip()
    app_id = os.environ.get("PIPELINEEDITOR_AZURE_CLIENT_ID", "").strip()
    secret = os.environ.get("PIPELINEEDITOR_AZURE_CLIENT_SECRET", "").strip()
    if tenant and app_id and secret:
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": app_id,
            "client_secret": secret,
            "scope": resource.rstrip("/") + "/.default",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))["access_token"]

    cid = client_id or AZURE_CLIENT_ID
    identity_endpoint = os.environ.get("IDENTITY_ENDPOINT")
    identity_header = os.environ.get("IDENTITY_HEADER")
    if identity_endpoint and identity_header:
        q = f"?resource={resource}&api-version=2019-08-01"
        if cid:
            q += f"&client_id={cid}"
        req = urllib.request.Request(
            identity_endpoint + q,
            headers={"X-IDENTITY-HEADER": identity_header},
        )
    else:
        q = f"?api-version=2018-02-01&resource={resource}"
        if cid:
            q += f"&client_id={cid}"
        req = urllib.request.Request(
            "http://169.254.169.254/metadata/identity/oauth2/token" + q,
            headers={"Metadata": "true"},
        )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))["access_token"]


from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: PLC0415, E402


def log(event: str, **fields):
    global _log
    if _log is None:
        _log = ChatHealthyLoggingService()
    entry = {"event": event, "host": _HOSTNAME}
    entry.update(fields)
    _log.info(json.dumps(entry, default=str))


def _legacy_blob_log_unused(event: str, **fields):
    """Prior append-blob logger (superseded). Kept as dead code marker
    so anyone reading git-blame sees the migration point. Not called
    from anywhere; may be deleted once the migration is confirmed
    end-to-end in prod.
    """
    try:
        day = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        blob_name = f"{PIPELINE_NAME}/{day}/watchdog.log"
        entry = {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "host": _HOSTNAME,
            "event": event,
        }
        entry.update(fields)
        line = json.dumps(entry, default=str) + "\n"
        tok = _get_token("https://storage.azure.com/")
        url = f"{LOG_ACCOUNT_URL}/{LOG_CONTAINER}/{blob_name}"
        for method, extra_headers in (
            ("PUT",  {"x-ms-blob-type": "AppendBlob", "Content-Length": "0"}),
            ("PUT",  {"x-ms-blob-type": "AppendBlob", "Content-Length": "0",
                      "If-None-Match": "*"}),
        ):
            try:
                r = urllib.request.Request(
                    url + "?comp=appendblock",
                    method="PUT",
                    data=line.encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {tok}",
                        "x-ms-version": "2020-04-08",
                        "x-ms-date": datetime.datetime.utcnow().strftime(
                            "%a, %d %b %Y %H:%M:%S GMT"),
                        "Content-Type": "application/json",
                    },
                )
                urllib.request.urlopen(r, timeout=5).read()
                return
            except urllib.error.HTTPError:
                try:
                    create = urllib.request.Request(
                        url,
                        method="PUT",
                        data=b"",
                        headers={
                            "Authorization": f"Bearer {tok}",
                            "x-ms-version": "2020-04-08",
                            "x-ms-blob-type": "AppendBlob",
                            "x-ms-date": datetime.datetime.utcnow().strftime(
                                "%a, %d %b %Y %H:%M:%S GMT"),
                        },
                    )
                    urllib.request.urlopen(create, timeout=5).read()
                except Exception:
                    pass
    except Exception:
        # Logging is best-effort; never crash the watchdog.
        pass


# -----------------------------------------------------------------------------
# Mongo helpers -- pipeline cluster (operator directive 2026-08-03: coord
# lives on pipeline cluster; frontend cluster is off-limits to pipeline).
# -----------------------------------------------------------------------------
def _mongo_client():
    return ChatHealthyMongoUtilities().getConnection("pipelineEditor", "admin")


# -----------------------------------------------------------------------------
# Azure ARM helpers -- VM enumeration, get-instance-view, delete
# -----------------------------------------------------------------------------
def _arm_get(url: str, tok: str) -> dict:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {tok}"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _arm_delete(url: str, tok: str) -> None:
    req = urllib.request.Request(
        url,
        method="DELETE",
        headers={"Authorization": f"Bearer {tok}"},
    )
    try:
        urllib.request.urlopen(req, timeout=60).read()
    except urllib.error.HTTPError as exc:
        if exc.code not in (200, 202, 204, 404):
            raise


def _list_pipeline_vms(tok: str) -> list[dict]:
    """Return every VM in the RG tagged pipeline_run_id."""
    url = (f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
           f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Compute/"
           f"virtualMachines?api-version=2024-03-01")
    resp = _arm_get(url, tok)
    out = []
    for vm in resp.get("value", []):
        tags = vm.get("tags") or {}
        if "pipeline_run_id" in tags:
            out.append(vm)
    return out


def _vm_power_state(vm_name: str, tok: str) -> str:
    url = (f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
           f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Compute/"
           f"virtualMachines/{vm_name}/instanceView?api-version=2024-03-01")
    try:
        resp = _arm_get(url, tok)
    except urllib.error.HTTPError as exc:
        return f"unknown_http_{exc.code}"
    for st in resp.get("statuses", []):
        code = st.get("code", "")
        if code.startswith("PowerState/"):
            return code.split("/", 1)[1]
    return "unknown"


def _vm_disk_names(vm: dict) -> list[str]:
    """Extract the OS disk name(s) and any attached data disk names from a
    VM inventory record. Used so the Watchdog can delete the disks along
    with the VM (deleteWithParent is not set by our current VM provision
    call, so a VM delete leaves its OS disk orphaned as Unattached)."""
    names: list[str] = []
    profile = ((vm.get("properties") or {}).get("storageProfile") or {})
    os_disk = profile.get("osDisk") or {}
    os_name = (os_disk.get("name") or "").strip()
    if os_name:
        names.append(os_name)
    for d in (profile.get("dataDisks") or []):
        dn = (d.get("name") or "").strip()
        if dn:
            names.append(dn)
    return names


def _delete_vm_nic_and_disks(vm: dict, tok: str) -> None:
    vm_name = vm["name"]
    disk_names = _vm_disk_names(vm)
    # Order: capture disk names FIRST (we have them from the inventory
    # record), then delete VM, then delete NIC, then delete disks. Delete
    # of the disk before or during VM delete would 409 Conflict; delete
    # after the VM release is safe.
    _arm_delete(
        (f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
         f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Compute/"
         f"virtualMachines/{vm_name}?api-version=2024-03-01"),
        tok,
    )
    _arm_delete(
        (f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
         f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/"
         f"networkInterfaces/{vm_name}-nic?api-version=2023-09-01"),
        tok,
    )
    for disk_name in disk_names:
        try:
            _arm_delete(
                (f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
                 f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Compute/"
                 f"disks/{disk_name}?api-version=2023-04-02"),
                tok,
            )
            log("delete_vm_disk", vm=vm_name, disk=disk_name)
        except Exception as exc:  # noqa: BLE001
            # Disk may already be gone or in a transient state; the orphan
            # disk sweep at end of run picks it up on the next tick.
            log("delete_vm_disk_error", vm=vm_name, disk=disk_name,
                error=f"{type(exc).__name__}: {exc}")


# Legacy name kept for callers below; forwards to the new-name function.
def _delete_vm_and_nic(vm: dict, tok: str) -> None:
    _delete_vm_nic_and_disks(vm, tok)


def _list_orphan_disks(tok: str) -> list[dict]:
    """Return every Unattached OS disk whose name matches the pipeline
    VM convention vm-chpipeline-*_OsDisk_*. These are OS disks whose
    parent VM was deleted (or reaped) without cascade-delete of the disk.
    Historical run-up: 35 disks x ~30 GB x $0.15/GB/mo = ~$150-200/mo
    of pure leak, seen 2026-07-29."""
    url = (f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
           f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Compute/"
           f"disks?api-version=2023-04-02")
    resp = _arm_get(url, tok)
    out = []
    for d in resp.get("value", []):
        name = d.get("name", "")
        state = ((d.get("properties") or {}).get("diskState") or "")
        if state == "Unattached" and name.startswith("vm-chpipeline-") and "_OsDisk_" in name:
            out.append(d)
    return out


def _reap_orphan_disks(tok: str) -> None:
    """Sweep at end of every watchdog tick. Catches disks whose parent
    VM was already reaped without the cascade OS-disk delete."""
    try:
        disks = _list_orphan_disks(tok)
    except Exception as exc:  # noqa: BLE001
        log("orphan_disk_sweep_list_error",
            error=f"{type(exc).__name__}: {exc}")
        return
    log("orphan_disk_sweep_inventory", count=len(disks),
        names=[d["name"] for d in disks])
    for d in disks:
        name = d["name"]
        try:
            _arm_delete(
                (f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
                 f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Compute/"
                 f"disks/{name}?api-version=2023-04-02"),
                tok,
            )
            log("orphan_disk_delete", disk=name)
        except Exception as exc:  # noqa: BLE001
            log("orphan_disk_delete_error", disk=name,
                error=f"{type(exc).__name__}: {exc}")


def _list_orphan_nics(tok: str) -> list[dict]:
    """Return every NIC in the RG that matches the pipeline VM naming
    convention (vm-chpipeline-*-nic) AND has no parent VM attached.
    Explicitly EXCLUDES the Atlas private-endpoint NIC (pe-atlas-*) -- that
    NIC has no virtualMachine binding by design and must never be deleted.
    Historical run-up: 82 orphan NICs observed 2026-08-03 accumulated
    over ~months of VM deletes that didn't cascade to their NICs."""
    url = (f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
           f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/"
           f"networkInterfaces?api-version=2023-09-01")
    resp = _arm_get(url, tok)
    out = []
    for n in resp.get("value", []):
        name = n.get("name", "")
        if not name.startswith("vm-chpipeline-") or not name.endswith("-nic"):
            continue
        vm_binding = ((n.get("properties") or {}).get("virtualMachine") or {}).get("id")
        if vm_binding:
            continue
        out.append(n)
    return out


def _reap_orphan_nics(tok: str) -> None:
    """Sweep at end of every watchdog tick. Catches NICs whose parent VM
    was deleted without a cascade NIC delete (older VM provisions did not
    set deleteOption on the NIC binding). Analog of _reap_orphan_disks."""
    try:
        nics = _list_orphan_nics(tok)
    except Exception as exc:  # noqa: BLE001
        log("orphan_nic_sweep_list_error",
            error=f"{type(exc).__name__}: {exc}")
        return
    log("orphan_nic_sweep_inventory", count=len(nics),
        names=[n["name"] for n in nics])
    for n in nics:
        name = n["name"]
        try:
            _arm_delete(
                (f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
                 f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Network/"
                 f"networkInterfaces/{name}?api-version=2023-09-01"),
                tok,
            )
            log("orphan_nic_delete", nic=name)
        except Exception as exc:  # noqa: BLE001
            log("orphan_nic_delete_error", nic=name,
                error=f"{type(exc).__name__}: {exc}")


# -----------------------------------------------------------------------------
# The 4-check state machine per v32 §3.1.2
# -----------------------------------------------------------------------------
TERMINAL_STATUSES = {"success", "failed", "aborted"}


def _reservation_still_live(mongo, run_id: str) -> bool:
    """Check 1 (v33 §3.1.2): reservation on front cluster's
    admin.cluster_lifecycle. The runbook writes {_id: run_id, expiry_at:
    NOW + 10h}; Controller finally deletes the row on quiesce. A row that
    exists and has expiry_at > NOW is a live claim -> Watchdog MUST NOT
    touch the VM. Missing row means Controller already finished or the
    reservation was reaped; err safe by returning False so subsequent
    manifest/heartbeat/grace checks decide."""
    now = datetime.datetime.utcnow()
    coll = mongo[PIPELINE_ADMIN_DB]["cluster_lifecycle"]
    r = coll.find_one({"_id": run_id})
    if not r:
        return False
    expiry_at = r.get("expiry_at")
    if isinstance(expiry_at, str):
        try:
            expiry_at = datetime.datetime.fromisoformat(
                expiry_at.replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except Exception:
            return True  # ambiguous -> err safe (don't kill)
    if not isinstance(expiry_at, datetime.datetime):
        return True
    return now < expiry_at


def _run_manifest(mongo, run_id: str) -> dict | None:
    return mongo[PIPELINE_ADMIN_DB]["pipeline.runs"].find_one(
        {"run_id": run_id}
    )


# Reaper design boundary (Skip, 2026-08-03): the reaper touches
# INFRASTRUCTURE ONLY. It does NOT touch data (collections, documents) and
# does NOT touch job metadata (pipeline.runs manifest, pipeline.work_items,
# admin.cluster_lifecycle, pipeline_lock). Data and job metadata are the
# pipeline's own concern -- Controller's finally block owns them at Level 1,
# and stale-lock self-heal in _acquire_pipeline_lock catches anything the
# Controller couldn't. The reaper's only job is to delete stalled VMs +
# their orphaned NICs + orphaned disks so infrastructure cost doesn't leak.


def _process_vm(vm: dict, mongo, tok: str) -> None:
    """Apply the 4-check state machine to a single VM."""
    vm_name = vm["name"]
    tags = vm.get("tags") or {}
    run_id = tags.get("pipeline_run_id", "")
    if not run_id:
        log("skip_vm_no_run_id_tag", vm=vm_name)
        return

    # Check 1: reservation live?
    if _reservation_still_live(mongo, run_id):
        log("skip_reservation_live", vm=vm_name, run_id=run_id)
        return

    manifest = _run_manifest(mongo, run_id)

    # Check 2: manifest terminal?
    if manifest and manifest.get("status") in TERMINAL_STATUSES:
        log("delete_terminal", vm=vm_name, run_id=run_id,
            status=manifest.get("status"))
        _delete_vm_and_nic(vm, tok)
        return

    # Check 3: heartbeat stale + PowerState?
    if manifest and manifest.get("status") in {"pending_vm_provision", "running"}:
        hb = manifest.get("controller_heartbeat_at")
        if isinstance(hb, str):
            try:
                hb = datetime.datetime.fromisoformat(hb.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                hb = None
        now = datetime.datetime.utcnow()
        stale = (
            hb is None
            or (now - hb).total_seconds() > HEARTBEAT_STALE_MINUTES * 60
        )
        if stale:
            power = _vm_power_state(vm_name, tok)
            if power in ("deallocated", "stopped"):
                abort = "controller_heartbeat_lost"
            elif power == "running":
                abort = "controller_process_dead"
            else:
                abort = f"controller_heartbeat_lost_power_{power}"
            log("delete_stale_heartbeat", vm=vm_name, run_id=run_id,
                power=power, abort_reason=abort)
            # Reaper only deletes the VM + orphaned NICs/disks. It does NOT
            # update pipeline.runs -- that's job metadata and stays untouched
            # per the reaper-infrastructure-only boundary. Next fire's
            # _acquire_pipeline_lock stale-lock self-heal handles freeing
            # the pipeline_lock; the manifest's status stays 'running' until
            # Controller (had it survived) or a separate metadata reaper
            # transitions it.
            _delete_vm_and_nic(vm, tok)
            return
        log("skip_heartbeat_fresh", vm=vm_name, run_id=run_id,
            heartbeat_age_seconds=int((now - hb).total_seconds()))
        return

    # Check 4: missing manifest + grace period passed?
    if not manifest:
        # Use the VM's provisioning time as the age proxy (tags don't carry
        # a timestamp; ARM provides timeCreated on the VM instance view).
        # Fall back to a fixed grace check: if the VM has existed longer
        # than MISSING_MANIFEST_GRACE_MINUTES, delete.
        # For simplicity: enumerate the VM's own creation time via ARM.
        try:
            iv = _arm_get(
                (f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
                 f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Compute/"
                 f"virtualMachines/{vm_name}?api-version=2024-03-01&"
                 f"$expand=instanceView"),
                tok,
            )
            time_created = iv.get("properties", {}).get("timeCreated", "")
            if time_created:
                created = datetime.datetime.fromisoformat(
                    time_created.replace("Z", "+00:00")
                ).replace(tzinfo=None)
                age_min = (datetime.datetime.utcnow() - created).total_seconds() / 60
                if age_min > MISSING_MANIFEST_GRACE_MINUTES:
                    log("delete_missing_manifest_past_grace",
                        vm=vm_name, run_id=run_id, age_min=age_min)
                    _delete_vm_and_nic(vm, tok)
                    return
                log("skip_missing_manifest_within_grace",
                    vm=vm_name, run_id=run_id, age_min=age_min)
                return
        except Exception as exc:  # noqa: BLE001
            log("missing_manifest_check_error", vm=vm_name, run_id=run_id,
                error=f"{type(exc).__name__}: {exc}")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main() -> int:
    # Wire Mongo logging FIRST - before any log() call. This fetches
    # MONGO_FRONTEND from KV, applies SRV->direct URI conversion (AA
    # sandbox cannot resolve _mongodb._tcp SRVs), and sets CHLS env so
    # every subsequent log() call also writes to Log_{env}.
    from chathealthy_lib.pipeline_boot import bootstrap_aa_mongo_logging  # noqa: PLC0415
    try:
        bootstrap_aa_mongo_logging(
            component_name="watchdog",
            env_prefix=ENV_PREFIX,
        )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            f"watchdog: bootstrap_aa_mongo_logging failed "
            f"({type(exc).__name__}: {exc}); continuing with stderr-only "
            "logging.\n"
        )
        os.environ.setdefault("CH_LOG_DESTINATION", "stderr")
    log("watchdog_start", host=_HOSTNAME)
    try:
        tok = _get_token("https://management.azure.com/")
    except Exception as exc:  # noqa: BLE001
        log("watchdog_fatal_no_arm_token", error=f"{type(exc).__name__}: {exc}")
        return 1
    try:
        mongo = _mongo_client()
        mongo.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        log("watchdog_fatal_no_mongo", error=f"{type(exc).__name__}: {exc}")
        return 1
    try:
        vms = _list_pipeline_vms(tok)
    except Exception as exc:  # noqa: BLE001
        log("watchdog_fatal_no_vm_list",
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc()[-1500:])
        return 1
    log("watchdog_vm_inventory", count=len(vms),
        names=[v["name"] for v in vms])
    for vm in vms:
        try:
            _process_vm(vm, mongo, tok)
        except Exception as exc:  # noqa: BLE001
            log("watchdog_vm_error",
                vm=vm.get("name", "?"),
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc()[-1500:])
    # Sweep any Unattached vm-chpipeline-*_OsDisk_* disks that outlived
    # their VM (pre-deleteOption VMs or edge cases where the cascade
    # delete lost a disk).
    _reap_orphan_disks(tok)
    # Same sweep for vm-chpipeline-*-nic NICs that outlived their VM.
    # Atlas PE NIC (pe-atlas-*) is explicitly excluded by the naming
    # prefix filter in _list_orphan_nics.
    _reap_orphan_nics(tok)
    log("watchdog_end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
