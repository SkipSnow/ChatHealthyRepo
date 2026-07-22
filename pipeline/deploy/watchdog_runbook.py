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
from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities

import datetime
import json
import os
import socket
import sys
import traceback
import urllib.error
import urllib.request


# CHLS env prerequisites for AA sandbox visibility. Set before any log()
# call so log output flows to stderr (captured by AA). Mongo destination
# is wired via bootstrap_aa_mongo_logging inside main() BEFORE the first
# log() call, so runbook events land in {env}_Pipelines.Log_{env}.
os.environ.setdefault("CH_SPACE_NAME", "watchdog")
os.environ.setdefault("ENV_PREFIX",
                      os.environ.get("AUTOMATION_ENV_PREFIX", "dev"))
os.environ.setdefault("CH_COMPONENT", "watchdog")


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


# -----------------------------------------------------------------------------
# Logging (append-blob, one blob per pipeline per day)
# -----------------------------------------------------------------------------
def log(event: str, **fields):
    """Append a one-line JSON event to the daily log blob. Never raises."""
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
        # Try append; if blob does not exist, create it as append-blob.
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
                # Blob may not exist yet -- create it, then retry append.
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
# Mongo helpers (front-end cluster only -- always-up)
# -----------------------------------------------------------------------------
def _get_mongo_conn_string() -> str:
    """Fetch MONGO_FRONTEND_connectionString from Key Vault via MI."""
    tok = _get_token("https://vault.azure.net")
    url = f"{KEY_VAULT_URI}secrets/MONGO-FRONTEND-connectionString?api-version=7.4"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {tok}"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))["value"]


def _mongo_client():
    import pymongo
    try:
        import certifi
        ca = certifi.where()
    except ImportError:
        ca = None
    conn = _get_mongo_conn_string()
    os.environ["MONGO_FRONTEND_connectionString"] = conn
    return ChatHealthyMongoUtilities("MONGO_FRONTEND_connectionString").getConnection()


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


def _delete_vm_and_nic(vm: dict, tok: str) -> None:
    vm_name = vm["name"]
    # Best-effort: also delete the NIC (name pattern from Runbook: <vm>-nic)
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
    coll = mongo["admin"]["cluster_lifecycle"]
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
    return mongo["chathealthyfrontend"]["pipeline.runs"].find_one(
        {"run_id": run_id}
    )


def _mark_run_failed(mongo, run_id: str, abort_reason: str) -> None:
    mongo["chathealthyfrontend"]["pipeline.runs"].update_one(
        {"run_id": run_id},
        {"$set": {
            "status": "failed",
            "abort_reason": abort_reason,
            "finished_at": datetime.datetime.utcnow().isoformat() + "Z",
        }},
    )


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
            _mark_run_failed(mongo, run_id, abort)
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
    # Wire Mongo logging FIRST — before any log() call. This fetches
    # MONGO_FRONTEND from KV, applies SRV->direct URI conversion (AA
    # sandbox cannot resolve _mongodb._tcp SRVs), and sets CHLS env so
    # every subsequent log() call also writes to Log_{env}.
    from chathealthy_frontend_lib.pipeline_boot import bootstrap_aa_mongo_logging  # noqa: PLC0415
    try:
        bootstrap_aa_mongo_logging(
            kv_uri=KEY_VAULT_URI,
            secret_name="MONGO-FRONTEND-connectionString",
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
    log("watchdog_end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
