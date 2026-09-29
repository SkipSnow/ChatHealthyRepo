# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Generic run-end release for the pipeline run lifecycle (EPIC-010-F-001-S-009).

Every pipeline's Controller ends the same way regardless of which pipeline it
is: it names any worker that died on its logging substrate, kills surviving
workers on a non-success exit, cancels the run's Atlas reservation, releases the
per-pipeline lock, marks the manifest terminal, flips orphaned work_items,
empties its staging on success, pauses the pipeline cluster, and deletes its own
VM. That sequence lives here so it is written once and every pipeline inherits
it. The provider-specific discrepancy report stays with the provider Controller,
where its inputs (the NPPES staging and provider collections) are known.
"""

from __future__ import annotations

import datetime
import os
import subprocess

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

_log = ChatHealthyLoggingService()


def fatal_on_worker_log_db_reports(run_id: str) -> None:
    """Find Workers that died because the logging substrate refused to start,
    and call it fatal. The Worker states the fact; this is where it becomes a
    verdict. Never raises -- it runs in the Controller's finally, and its own
    failure must not displace the outcome already being recorded.
    """
    if not run_id:
        return
    try:
        from pipeline.run_lifecycle.pipeline_fatal_recorder import record_fatal_discrepancy  # noqa: PLC0415
        wi = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")
        rows = list(wi["pipelineAdmin"]["pipeline.work_items"].find(
            {"run_id": run_id, "reason": {"$regex": "^log_db_fatal:"}},
            {"step": 1, "reason": 1, "detail": 1},
        ))
        for row in rows:
            record_fatal_discrepancy(
                wi,
                run_id=run_id,
                step=str(row.get("step") or ""),
                exc=ChatHealthyException(
                    mode="worker_log_db_fatal",
                    component="ControlRunner",
                    message=(
                        f"Worker step={row.get('step')!r} could not start its "
                        f"logging substrate: {row.get('reason')}. "
                        f"{row.get('detail', '')}"
                    ),
                ),
            )
        if rows:
            _log.LogPipeline("ERROR", 
                "control_runner: %d worker(s) failed on log db; run is fatal",
                len(rows),
            )
    except Exception:
        pass


def kill_active_workers() -> None:
    """SIGTERM every descendant process of this Controller. Container tear-
    down would eventually kill them anyway, but we want them stopped
    promptly on abend so they don't complete additional LLM calls / DB
    writes after the run is already failed."""
    import signal  # noqa: PLC0415
    try:
        # pgrep -P <pid> lists direct children; iterate depth-first so we
        # cover workers that in turn spawned helpers.
        my_pid = os.getpid()
        seen: set[int] = set()
        stack = [my_pid]
        killed: list[int] = []
        while stack:
            parent = stack.pop()
            try:
                out = subprocess.run(
                    ["pgrep", "-P", str(parent)],
                    capture_output=True, text=True, timeout=5,
                )
                for line in out.stdout.splitlines():
                    line = line.strip()
                    if not line.isdigit():
                        continue
                    child = int(line)
                    if child in seen or child == my_pid:
                        continue
                    seen.add(child)
                    stack.append(child)
                    try:
                        os.kill(child, signal.SIGTERM)
                        killed.append(child)
                    except (ProcessLookupError, PermissionError):
                        continue
            except (FileNotFoundError, subprocess.TimeoutExpired):
                # pgrep unavailable (unlikely in the Ubuntu container) or
                # timed out. Container tear-down will still clean up.
                break
        if killed:
            _log.LogPipeline("INFO", "control_runner: SIGTERM sent to %d worker pid(s): %s",
                       len(killed), killed)
    except Exception as exc:  # noqa: BLE001
        _log.LogPipeline("WARNING", "control_runner: _kill_active_workers failed: %s",
                     type(exc).__name__)


def pause_pipeline_cluster() -> None:
    """Pause the pipeline cluster via Atlas API. Best-effort; failure does
    not block VM deletion. The reaper is the fallback if this fails."""
    try:
        import requests  # noqa: PLC0415
        from requests.auth import HTTPDigestAuth  # noqa: PLC0415
    except ImportError:
        _log.LogPipeline("WARNING", "quiesce: requests not available; cluster pause skipped")
        return

    pub_key = os.environ.get("ATLAS_PIPELINE_PUBLIC_KEY", "").strip()
    priv_key = os.environ.get("ATLAS_PIPELINE_PRIVATE_KEY", "").strip()
    project_id = os.environ.get("ATLAS_PROJECT_ID", "").strip()
    cluster_name = os.environ.get("PIPELINE_CLUSTER", "chathealthypipeline").strip()

    if not (pub_key and priv_key and project_id):
        _log.LogPipeline("WARNING", "quiesce: Atlas credentials not configured; cluster pause skipped")
        return

    try:
        auth = HTTPDigestAuth(pub_key, priv_key)
        url = f"https://cloud.mongodb.com/api/atlas/v2/groups/{project_id}/clusters/{cluster_name}"
        resp = requests.patch(
            url,
            json={"paused": True},
            auth=auth,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code in (200, 202):
            _log.LogPipeline("INFO", "quiesce: pipeline cluster paused")
        else:
            _log.LogPipeline("WARNING", "quiesce: cluster pause rejected (%d): %s",
                        resp.status_code, resp.text[:200])
    except Exception as exc:
        _log.LogPipeline("WARNING", "quiesce: cluster pause failed (reaper will retry): %s", exc)


_STAGING_DB = "PublicStaging"


def empty_pipeline_staging() -> None:
    """A successful run's staging is spent scratch; after it, PublicStaging must
    be empty. Lists every collection in the pipeline cluster's PublicStaging DB
    and drops them all. Called only on a 'succeeded' exit -- a failed run keeps
    its staging for diagnosis until the next success clears it. pipelineEditor
    owns PublicStaging end to end. Best-effort in the finally: a failure here is
    logged and does not displace the run's outcome, and the cluster pause that
    follows still runs."""
    try:
        staging = ChatHealthyMongoUtilities().getConnection(
            "pipelineEditor", "ChatHealthyDataPipelines")[_STAGING_DB]
        dropped = 0
        for coll in staging.list_collection_names():
            staging.drop_collection(coll)
            dropped += 1
        _log.LogPipeline("INFO", "quiesce: %s emptied on success; %d collection(s) dropped",
                  _STAGING_DB, dropped)
    except Exception as exc:  # noqa: BLE001
        _log.LogPipeline("ERROR", "quiesce: staging empty FAILED err=%s", str(exc)[:500])


def fire_farewell_vm_delete() -> None:
    """Delete this run's VM through ARM, with the credential already in hand.

    This used to shell out to `az vm delete`, and the container has no Azure
    CLI -- Dockerfile.control installs pip packages and nothing else. Every
    call raised FileNotFoundError, logged a warning nobody read, and left a
    32-core host running until a human noticed. One such host consumed the
    regional quota and refused the next run outright.

    ARM is reached directly instead: requests and azure-identity are both in
    the image, and the identity is the same pipelineEditor service principal
    the rest of the container authenticates with. The DELETE returns 202 and
    ARM finishes asynchronously, so this never blocks the exit.
    """
    if os.environ.get("PIPELINE_LOCAL_MODE", "").strip() == "1":
        _log.LogPipeline("INFO", "control_runner: local mode, no host to delete")
        return

    subscription = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    rg = os.environ.get("AZURE_RESOURCE_GROUP", "").strip()
    vm_name = os.environ.get("AZURE_VM_NAME", "").strip()
    if not vm_name:
        run_id = os.environ.get("RUN_ID", "").strip()
        if run_id:
            short = run_id.split("-")[-1][:8] if "-" in run_id else run_id[:8]
            vm_name = f"vm-chpipeline-{short}"
    missing = [n for n, v in (("AZURE_SUBSCRIPTION_ID", subscription),
                              ("AZURE_RESOURCE_GROUP", rg),
                              ("AZURE_VM_NAME/RUN_ID", vm_name)) if not v]
    if missing:
        _log.LogPipeline("ERROR", "control_runner: cannot delete this host, %s absent; "
                   "the watchdog must reap it", ", ".join(missing))
        return

    try:
        import requests  # noqa: PLC0415
        from pipeline.run_lifecycle.pipeline_identity import pipeline_editor_credential  # noqa: PLC0415

        credential = pipeline_editor_credential()
        token = credential.get_token("https://management.azure.com/.default").token
        url = (f"https://management.azure.com/subscriptions/{subscription}"
               f"/resourceGroups/{rg}/providers/Microsoft.Compute"
               f"/virtualMachines/{vm_name}?api-version=2024-07-01"
               f"&forceDeletion=true")
        response = requests.delete(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if response.status_code in (200, 202, 204, 404):
            _log.LogPipeline("INFO", "control_runner: host %s delete accepted (HTTP %d)",
                      vm_name, response.status_code)
        else:
            _log.LogPipeline("ERROR", "control_runner: host %s delete refused (HTTP %d: %s); "
                       "the watchdog must reap it", vm_name,
                       response.status_code, response.text[:200])
    except Exception as exc:  # noqa: BLE001
        _log.LogPipeline("ERROR", "control_runner: host %s delete failed (%s: %s); "
                   "the watchdog must reap it",
                   vm_name, type(exc).__name__, str(exc)[:200])


def quiesce_mongo_state(run_id: str, final_status: str) -> None:
    """Cancel the pipeline-run reservation on the front cluster, release the
    per-pipeline lock, mark the run manifest terminal, and on a non-success
    exit flip in-flight work_items to failed. Best-effort per step; a failure
    in one MUST NOT block the others, and none block the subsequent VM delete.
    The discrepancy report is emitted separately by the pipeline's own
    Controller, which alone knows what its rows mean."""
    if not run_id:
        _log.LogPipeline("WARNING", "quiesce_mongo_state: no run_id available; skipping")
        return
    try:
        mongo = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")
    except Exception as exc:
        _log.LogPipeline("ERROR", "quiesce: unable to open pipeline-cluster Mongo run_id=%s err=%s",
                   run_id, str(exc)[:500])
        return
    try:
        mongo["pipelineAdmin"]["cluster_lifecycle"].delete_one({"_id": run_id})
        _log.LogPipeline("INFO", "quiesce: reservation cancelled run_id=%s", run_id)
    except Exception as exc:
        _log.LogPipeline("ERROR", "quiesce: reservation cancel FAILED run_id=%s err=%s",
                   run_id, str(exc)[:500])
    # Release the per-pipeline mutual-exclusion lock. Runbook acquired
    # it at fire-start; Controller inherits ownership when the VM boots
    # and MUST release it here so the next fire for the same
    # pipeline_name is not blocked. Guarded by run_id so a duplicate
    # fire's Controller (theoretically impossible but belt-and-suspenders)
    # can never clobber the real holder's lock.
    pipeline_name = os.environ.get("PIPELINE_NAME", "")
    if pipeline_name:
        try:
            r = mongo["pipelineAdmin"]["cluster_lifecycle"].delete_one({
                "_id": f"pipeline_lock:{pipeline_name}",
                "run_id": run_id,
            })
            _log.LogPipeline("INFO", 
                "quiesce: pipeline_lock released pipeline=%s run_id=%s deleted=%d",
                pipeline_name, run_id, r.deleted_count,
            )
        except Exception as exc:
            _log.LogPipeline("ERROR", 
                "quiesce: pipeline_lock release FAILED pipeline=%s run_id=%s err=%s",
                pipeline_name, run_id, str(exc)[:500],
            )
    try:
        mongo["pipelineAdmin"]["pipeline.runs"].update_one(
            {"run_id": run_id},
            {"$set": {
                "status": final_status,
                "ended_at": datetime.datetime.utcnow(),
            }},
        )
        _log.LogPipeline("INFO", "quiesce: manifest marked terminal run_id=%s status=%s",
                  run_id, final_status)
    except Exception as exc:
        _log.LogPipeline("ERROR", "quiesce: manifest update FAILED run_id=%s err=%s",
                   run_id, str(exc)[:500])
    # On any non-success terminal status, flip this run's in-flight
    # work_items to failed so no zombies persist. A successful run has
    # already flipped its own work_items via the orchestrator's normal
    # completion path.
    if final_status != "succeeded":
        try:
            res = mongo["pipelineAdmin"]["pipeline.work_items"].update_many(
                {"run_id": run_id,
                 "status": {"$nin": ["completed", "done", "failed"]}},
                {"$set": {
                    "status": "failed",
                    "abort_reason": f"controller_quiesce_{final_status}",
                    "failed_at": datetime.datetime.utcnow(),
                }},
            )
            if res.modified_count:
                _log.LogPipeline("INFO", 
                    "quiesce: work_items flipped run_id=%s count=%d",
                    run_id, res.modified_count,
                )
        except Exception as exc:
            _log.LogPipeline("ERROR", "quiesce: work_items flip FAILED run_id=%s err=%s",
                       run_id, str(exc)[:500])
