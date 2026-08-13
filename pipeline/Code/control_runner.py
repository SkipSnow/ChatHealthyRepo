# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Provider Pipeline LLD v32 §3.1.4 / §5.2 — Controller entry point.

Runs inside the pipeline docker image on the Pipeline Run VM. The VM was
created by the Runbook (v32 §5.2.2) with cloud-init user_data that pulls
the image and invokes this module:

    python control_runner.py --run-id ... --env-prefix ...

The Runbook has already written a fresh run manifest to
pipelineAdmin.pipeline.runs with status=pending_vm_provision.
This process:
  1. Reads the manifest via Mongo Atlas Private Endpoint (X.509 identity
     from the Controller's F-003 cert, fetched from KV via the VM's MI).
  2. Updates the manifest to status=running + controller_heartbeat_at.
  3. Walks the STEPS list; spawns Workers as subprocesses.
  4. On terminal state (success/failed/aborted), quiesces:
     - Writes final manifest state
     - Fires `az vm delete --no-wait` on the current VM (farewell script)
     - Exits — cloud-init container ends, VM is destroyed by ARM.

Local runs (developer workstation) skip the VM-teardown step (they
detect PIPELINE_LOCAL_MODE=1 in env).
"""

from __future__ import annotations

import os

# CH_LOG_DESTINATION MUST be set before the first ChatHealthyLoggingService
# call — CHLS caches the destination binding on first _emit() and any later
# env change is ignored. Set to "stderr,mongo" so Controller logs reach both
# docker stdout AND Pipelines.Log_{env}. Other runbooks (reservation_reaper,
# migrator, change_db_version) all set this at module load for the same
# reason; control_runner was missing it, which is why the log was empty for
# every prior pipeline run.
os.environ.setdefault("CH_LOG_DESTINATION", "stderr,mongo")
os.environ.setdefault("CH_SPACE_NAME", "controller")
os.environ.setdefault("CH_COMPONENT", "provider_pipeline_control")

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

import argparse
import datetime
import json
import subprocess
import sys

from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

from blob_client import get_blob_service
from pipeline_db import get_mongo, PIPELINE_ADMIN_DB
from pipeline_env import load_pipeline_env
from provider_pipeline_orchestrator import ProviderPipelineOrchestrator
from step_context import PipelineArgs

_log = ChatHealthyLoggingService()


def _fire_farewell_vm_delete() -> None:
    """v32 §5.2.20 quiesce step 6: fire `az vm delete --no-wait` on the
    current VM. ARM destroys the VM within 30-60s. Never blocks.

    In local mode (PIPELINE_LOCAL_MODE=1) this is a no-op — nothing to
    tear down.

    The VM name is read from the AZURE_VM_NAME env var (set by cloud-init
    at boot time from the metadata service) OR derived from the RUN_ID
    (Runbook naming convention: vm-chpipeline-<run_id_short>).
    """
    if os.environ.get("PIPELINE_LOCAL_MODE", "").strip() == "1":
        _log.info("control_runner: local mode — skipping VM delete")
        return
    rg = os.environ.get("AZURE_RESOURCE_GROUP", "").strip()
    if not rg:
        _log.warning("control_runner: AZURE_RESOURCE_GROUP not set — cannot fire VM delete")
        return
    # Prefer explicit env var; otherwise derive from RUN_ID
    vm_name = os.environ.get("AZURE_VM_NAME", "").strip()
    if not vm_name:
        run_id = os.environ.get("RUN_ID", "").strip()
        if not run_id:
            _log.warning("control_runner: neither AZURE_VM_NAME nor RUN_ID set — cannot fire VM delete")
            return
        short_id = run_id.split("-")[-1][:8] if "-" in run_id else run_id[:8]
        vm_name = f"vm-chpipeline-{short_id}"
    # Fire-and-forget: async delete via ARM. The `--no-wait` flag returns
    # immediately; the current process can then exit cleanly. Once the
    # container process ends, cloud-init has nothing more to run.
    try:
        # start_new_session reparents the child to init, which reaps it.
        # Without it this Popen is never waited on -- `az` is a shell script,
        # so it left an `sh` zombie parented to the Controller for the rest of
        # the run. The delete still returns immediately; nothing here blocks.
        subprocess.Popen(
            ["az", "vm", "delete", "--yes", "--no-wait",
             "--resource-group", rg, "--name", vm_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
        _log.info("control_runner: fired az vm delete --no-wait for %s", vm_name)
    except FileNotFoundError:
        _log.warning("control_runner: az CLI not on PATH; VM %s will be reaped by Watchdog", vm_name)
    except Exception as exc:  # noqa: BLE001
        _log.warning("control_runner: farewell VM-delete failed for %s: %s (Watchdog will reap)", vm_name, exc)


def _states_default_from_env() -> str:
    """Runbook publishes STATE_SCOPE as JSON-encoded list. If present,
    that is the operator's chosen scope; use it. Otherwise fall back to
    STATES env, otherwise "ALL"."""
    raw = os.environ.get("STATE_SCOPE", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return ",".join(str(s) for s in parsed) or "ALL"
        if isinstance(parsed, str):
            return parsed or "ALL"
    return os.environ.get("STATES", "ALL")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provider Pipeline Control runner")
    parser.add_argument("--run-id", dest="run_id",
                        default=os.environ.get("RUN_ID") or None,
                        help="Existing manifest run_id; new one is minted if omitted")
    parser.add_argument("--env-prefix", dest="env_prefix",
                        default=os.environ.get("ENV_PREFIX", "dev"),
                        help="Environment prefix (local|dev|qa|prod)")
    parser.add_argument("--states", dest="states",
                        default=_states_default_from_env(),
                        help="Comma-separated state list or ALL. Defaults from "
                             "STATE_SCOPE env (runbook publishes as JSON list).")
    parser.add_argument("--load-mode", dest="load_mode",
                        default=os.environ.get("LOAD_MODE", "full"),
                        choices=["full", "incremental"],
                        help="Full load or incremental. Defaults from LOAD_MODE env.")
    parser.add_argument("--resume-from-step", dest="resume_from_step",
                        default=os.environ.get("RESUME_FROM_STEP") or None,
                        help="Skip completed steps up to this step name. "
                             "Defaults from RESUME_FROM_STEP env.")
    parser.add_argument("--expected-duration-minutes",
                        dest="expected_duration_minutes",
                        type=int,
                        default=int(os.environ.get("EXPECTED_DURATION_MINUTES", "120")))
    parser.add_argument("--log-level", dest="log_level",
                        default=os.environ.get("LOG_LEVEL", "INFO"))
    # Mandatory. May come from --data-version CLI arg or DATA_VERSION env.
    # PipelineArgs.__post_init__ enforces int >= 1.
    _dv_env = os.environ.get("DATA_VERSION", "").strip()
    _dv_default = int(_dv_env) if _dv_env.isdigit() else None
    parser.add_argument("--data-version", dest="data_version", type=int,
                        default=_dv_default, required=(_dv_default is None),
                        help="Provider collection version number "
                             "(e.g. 3 -> Provider_v_3). MANDATORY. Reads "
                             "DATA_VERSION env if flag omitted.")
    # Optional. Enables the paid Google Maps terminal stage in the
    # county-enrichment cascade (LLD §4.13 stage 4). Off by default; on
    # via --google-maps-enabled or GOOGLE_MAPS_ENABLED env in {1,true,yes}.
    # Requires GOOGLE_MAPS_API_KEY in the environment when enabled.
    _gm_env = os.environ.get("GOOGLE_MAPS_ENABLED", "").strip().lower()
    _gm_default = _gm_env in ("1", "true", "yes")
    parser.add_argument("--google-maps-enabled", dest="google_maps_enabled",
                        action="store_true", default=_gm_default,
                        help="Enable the paid Google Maps stage in the "
                             "county-enrichment cascade. Reads "
                             "GOOGLE_MAPS_ENABLED env if flag omitted "
                             "({1,true,yes}=on).")
    return parser.parse_args(argv)


def _states_list(raw: str) -> list[str]:
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv if argv is not None else sys.argv[1:])

    if ns.log_level:
        os.environ.setdefault("LOG_LEVEL", ns.log_level.upper())

    load_pipeline_env()

    states_list = _states_list(ns.states)
    incremental = (ns.load_mode == "incremental")
    # Export data_version to env so Worker subprocesses (spawned via
    # spawn_detached_worker with the current os.environ.copy()) inherit
    # it, and so CHLS _MongoLogHandler.emit() picks it up on every log
    # line (see logging_service.py "data_version" doc field).
    os.environ["DATA_VERSION"] = str(ns.data_version)
    os.environ["GOOGLE_MAPS_ENABLED"] = "1" if ns.google_maps_enabled else "0"
    args = PipelineArgs(
        states=states_list,
        env_prefix=ns.env_prefix,
        expected_duration_minutes=ns.expected_duration_minutes,
        resume_from_step=ns.resume_from_step,
        run_id=ns.run_id,
        incremental=incremental,
        data_version=ns.data_version,
        google_maps_enabled=ns.google_maps_enabled,
    )

    from pipeline_config import load_pipeline_config
    orchestrator = ProviderPipelineOrchestrator(
        env=ns.env_prefix,
        config=load_pipeline_config(env_prefix=ns.env_prefix),
        mongo_client=get_mongo(),
        blob_client=get_blob_service(),
    )
    if ns.run_id:
        os.environ["RUN_ID"] = ns.run_id
    _log.info(
        "control_runner: starting run env_prefix=%s states=%s load_mode=%s "
        "resume_from_step=%s expected_duration_minutes=%d run_id=%s "
        "STATE_SCOPE_env=%r LOAD_MODE_env=%r",
        ns.env_prefix, states_list, ns.load_mode, ns.resume_from_step,
        ns.expected_duration_minutes, ns.run_id or "(mint)",
        os.environ.get("STATE_SCOPE", ""), os.environ.get("LOAD_MODE", ""),
    )
    # v32 §5.2.20 quiesce order: cancel reservation -> mark manifest
    # terminal -> az vm delete. Mongo cleanup MUST complete before VM
    # delete; the VM going away is the last step so any earlier failure
    # surfaces in Mongo before the machine that could have retried it
    # is gone.
    manifest = None
    exit_code = 1
    final_status = "failed"

    # Controller heartbeat: writes pipelineAdmin.pipeline.runs
    # controller_heartbeat_at every 60s in a daemon thread. Watchdog
    # reads this to detect Controller-dead-without-quiesce (see LLD
    # §3.1.2 step 3). Thread dies with the process; the daemon flag
    # ensures it does not keep Controller alive past its own exit.
    # Operator directive 2026-08-03: coord on pipeline cluster only.
    import threading  # noqa: PLC0415
    _hb_stop = threading.Event()
    _RENEWAL_HOURS = 2  # each heartbeat pushes expiry_at 2h into the future
    def _heartbeat() -> None:
        rid = ns.run_id or os.environ.get("RUN_ID", "")
        if not rid:
            return
        while not _hb_stop.wait(60):
            try:
                m = ChatHealthyMongoUtilities().getConnection(
                    "pipelineEditor", "admin"
                )
                now = datetime.datetime.utcnow()
                m[PIPELINE_ADMIN_DB]["pipeline.runs"].update_one(
                    {"run_id": rid},
                    {"$set": {"controller_heartbeat_at": now}},
                )
                # Extend reservation expiry_at so a long-running step past
                # the initial 10h TTL is not reaped by reservation_reaper.
                # Reaper reads expiry_at (post-fix); Watchdog reads
                # controller_heartbeat_at on the manifest.
                m[PIPELINE_ADMIN_DB]["cluster_lifecycle"].update_one(
                    {"_id": rid},
                    {"$set": {"expiry_at": now + datetime.timedelta(hours=_RENEWAL_HOURS)}},
                )
            except Exception as exc:
                _log.warning("controller heartbeat write failed run_id=%s err=%s",
                             rid, str(exc)[:200])
    threading.Thread(target=_heartbeat, daemon=True, name="controller-heartbeat").start()

    manifest = None
    final_status = "failed"
    exit_code = 1
    fatal_exception = None

    try:
        manifest = orchestrator.run(args)
        if manifest and manifest.run_id:
            os.environ["RUN_ID"] = manifest.run_id
        final_status = manifest.status if manifest else "failed"
        _log.info("control_runner: run %s finished status=%s",
                  manifest.run_id if manifest else "(none)", final_status)
        exit_code = 0 if final_status == "succeeded" else 1
    except ChatHealthyException as ch_exc:
        fatal_exception = ch_exc
        final_status = "failed"
        _log.error("control_runner: fatal exception during orchestration: %s", ch_exc, exc=ch_exc)
    except Exception as other_exc:
        fatal_exception = other_exc
        final_status = "failed"
        _log.error(
            "control_runner: fatal exception during orchestration: %s",
            other_exc,
            exc=ChatHealthyException(
                mode="orchestration_failure",
                message=f"Fatal exception during orchestration: {other_exc}",
                component="ControlRunner",
                exception=other_exc,
            ),
        )
    finally:
        # On any non-success terminal state, kill every child process the
        # Controller spawned (worker subprocesses). Operator directive
        # 2026-08-03: "if we get a fatal error the controller must kill any
        # active workers". Prevents LLM/DB writes from continuing after the
        # run has already abended.
        # Workers never declare a run fatal; they state what happened to their
        # own work item and stop. The Controller reads those reports and is the
        # one that calls it. Runs before the kill so the reason is recorded
        # while the evidence is still there.
        _fatal_on_worker_log_db_reports(
            (manifest.run_id if manifest and manifest.run_id else None)
            or os.environ.get("RUN_ID", "")
        )
        if final_status != "succeeded":
            _kill_active_workers()
        run_id_for_quiesce = (
            (manifest.run_id if manifest and manifest.run_id else None)
            or os.environ.get("RUN_ID", "")
        )
        _quiesce_mongo_state(run_id_for_quiesce, final_status,
                              manifest=manifest, args=args, fatal_exception=fatal_exception)
        if manifest:
            _pause_pipeline_cluster()
            _fire_farewell_vm_delete()
    return exit_code


def _fatal_on_worker_log_db_reports(run_id: str) -> None:
    """Find Workers that died because the logging substrate refused to start,
    and call it fatal. The Worker states the fact; this is where it becomes a
    verdict. Never raises -- it runs in the Controller's finally, and its own
    failure must not displace the outcome already being recorded.
    """
    if not run_id:
        return
    try:
        from pipeline_fatal_recorder import record_fatal_discrepancy
        wi = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "admin")
        rows = list(wi[PIPELINE_ADMIN_DB]["pipeline.work_items"].find(
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
            _log.error(
                "control_runner: %d worker(s) failed on log db; run is fatal",
                len(rows),
            )
    except Exception:
        pass


def _kill_active_workers() -> None:
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
            _log.info("control_runner: SIGTERM sent to %d worker pid(s): %s",
                       len(killed), killed)
    except Exception as exc:  # noqa: BLE001
        _log.warning("control_runner: _kill_active_workers failed: %s",
                     type(exc).__name__)


def _pause_pipeline_cluster() -> None:
    """Pause the pipeline cluster via Atlas API. Best-effort; failure does
    not block VM deletion. The reaper is the fallback if this fails."""
    try:
        import requests  # noqa: PLC0415
        from requests.auth import HTTPDigestAuth  # noqa: PLC0415
    except ImportError:
        _log.warning("quiesce: requests not available; cluster pause skipped")
        return

    pub_key = os.environ.get("ATLAS_PIPELINE_PUBLIC_KEY", "").strip()
    priv_key = os.environ.get("ATLAS_PIPELINE_PRIVATE_KEY", "").strip()
    project_id = os.environ.get("ATLAS_PROJECT_ID", "").strip()
    cluster_name = os.environ.get("PIPELINE_CLUSTER", "chathealthypipeline").strip()

    if not (pub_key and priv_key and project_id):
        _log.warning("quiesce: Atlas credentials not configured; cluster pause skipped")
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
            _log.info("quiesce: pipeline cluster paused")
        else:
            _log.warning("quiesce: cluster pause rejected (%d): %s",
                        resp.status_code, resp.text[:200])
    except Exception as exc:
        _log.warning("quiesce: cluster pause failed (reaper will retry): %s", exc)


def _quiesce_mongo_state(run_id: str, final_status: str, *,
                          manifest=None, args=None, fatal_exception=None) -> None:
    """v32 §5.2.20 quiesce steps 1-2 + 2026-08-03 addition: cancel the
    pipeline-run reservation on the front cluster, mark the run manifest
    terminal, AND unconditionally emit the discrepancy report with any fatal
    exception details. Best-effort per step; a failure in one MUST NOT block
    the other, and none block the subsequent VM delete."""
    if not run_id:
        _log.warning("quiesce_mongo_state: no run_id available; skipping")
        return
    try:
        mongo = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "admin")
    except Exception as exc:
        _log.error("quiesce: unable to open pipeline-cluster Mongo run_id=%s err=%s",
                   run_id, str(exc)[:500])
        return
    try:
        mongo[PIPELINE_ADMIN_DB]["cluster_lifecycle"].delete_one({"_id": run_id})
        _log.info("quiesce: reservation cancelled run_id=%s", run_id)
    except Exception as exc:
        _log.error("quiesce: reservation cancel FAILED run_id=%s err=%s",
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
            r = mongo[PIPELINE_ADMIN_DB]["cluster_lifecycle"].delete_one({
                "_id": f"pipeline_lock:{pipeline_name}",
                "run_id": run_id,
            })
            _log.info(
                "quiesce: pipeline_lock released pipeline=%s run_id=%s deleted=%d",
                pipeline_name, run_id, r.deleted_count,
            )
        except Exception as exc:
            _log.error(
                "quiesce: pipeline_lock release FAILED pipeline=%s run_id=%s err=%s",
                pipeline_name, run_id, str(exc)[:500],
            )
    try:
        mongo[PIPELINE_ADMIN_DB]["pipeline.runs"].update_one(
            {"run_id": run_id},
            {"$set": {
                "status": final_status,
                "ended_at": datetime.datetime.utcnow(),
            }},
        )
        _log.info("quiesce: manifest marked terminal run_id=%s status=%s",
                  run_id, final_status)
    except Exception as exc:
        _log.error("quiesce: manifest update FAILED run_id=%s err=%s",
                   run_id, str(exc)[:500])
    # On any non-success terminal status, flip this run's in-flight
    # work_items to failed so no zombies persist. A successful run has
    # already flipped its own work_items via the orchestrator's normal
    # completion path.
    if final_status != "succeeded":
        try:
            res = mongo[PIPELINE_ADMIN_DB]["pipeline.work_items"].update_many(
                {"run_id": run_id,
                 "status": {"$nin": ["completed", "done", "failed"]}},
                {"$set": {
                    "status": "failed",
                    "abort_reason": f"controller_quiesce_{final_status}",
                    "failed_at": datetime.datetime.utcnow(),
                }},
            )
            if res.modified_count:
                _log.info(
                    "quiesce: work_items flipped run_id=%s count=%d",
                    run_id, res.modified_count,
                )
        except Exception as exc:
            _log.error("quiesce: work_items flip FAILED run_id=%s err=%s",
                       run_id, str(exc)[:500])

    # Discrepancy report — ALWAYS emit, perfect run OR abend. Operator
    # directive 2026-08-03: "we always in every case, even with a perfect
    # job or an abend get the discrepancy report". Best-effort so a
    # mongo/SparkPost outage doesn't block the reservation release.
    try:
        pipeline_mongo = None
        try:
            pipeline_mongo = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "admin")
        except Exception as mongo_exc:
            _log.error("quiesce: mongo unreachable for discrepancy report run_id=%s err=%s",
                       run_id, str(mongo_exc)[:500])
            fatal_exception = fatal_exception or mongo_exc

        if pipeline_mongo:
            # Mongo is reachable - use normal emit_discrepancy_report path
            from steps.discrepancy_report import emit_discrepancy_report  # noqa: PLC0415
            from pipeline_config import load_pipeline_config  # noqa: PLC0415
            env_prefix = os.environ.get("ENV_PREFIX", "dev")
            cfg = load_pipeline_config(mongo_client=None, env_prefix=env_prefix)
            manifest_status = manifest.status if manifest else final_status
            manifest_doc = (
                manifest.to_document()
                if manifest and hasattr(manifest, "to_document") else
                {"run_id": run_id, "status": manifest_status}
            )
            if fatal_exception:
                manifest_doc["fatal_exception"] = {
                    "type": type(fatal_exception).__name__,
                    "message": str(fatal_exception),
                    "mode": getattr(fatal_exception, "mode", "unknown"),
                }
            summary = emit_discrepancy_report(
                pipeline_mongo=pipeline_mongo,
                run_id=run_id,
                manifest_status=manifest_status,
                manifest_doc=manifest_doc,
                config=cfg,
                operator_email=getattr(args, "operator_email", None) if args else None,
                operator_sms=getattr(args, "operator_sms", None) if args else None,
            )
            _log.info(
                "quiesce: discrepancy report emitted run_id=%s total=%d pdf_bytes=%d",
                run_id, summary.get("total", 0), summary.get("pdf_bytes", 0),
            )
        else:
            # Mongo unreachable - emit minimal report to stderr
            _log.error(
                "quiesce: DISCREPANCY REPORT run_id=%s status=%s fatal_exception=%s",
                run_id, final_status,
                f"{type(fatal_exception).__name__}: {fatal_exception}" if fatal_exception else "None"
            )
    except Exception as exc:
        _log.error("quiesce: discrepancy report FAILED run_id=%s err=%s",
                   run_id, str(exc)[:500])


if __name__ == "__main__":
    raise SystemExit(main())
