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
  4. On terminal state (success/failed/aborted), quiesces via the generic
     run-end release in pipeline.run_lifecycle.controller_release, then emits
     the provider-specific discrepancy report and exits — cloud-init container
     ends, VM is destroyed by ARM.

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

from chathealthy_lib.logging_service import (
    ChatHealthyLoggingService, set_mongo_log_identity)
from chathealthy_lib.exceptions import ChatHealthyException

# The identity is named here because CH_LOG_DESTINATION above asks for mongo,
# and the mongo handler refuses to wire without one. It used to arrive as a
# side effect of importing pipeline_db; that import went away with the helpers
# it existed for, and took the identity with it, so the first _log call died.
set_mongo_log_identity("pipelineEditor")

import argparse
import datetime
import json
import sys

from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

from pipeline.run_lifecycle.blob_client import get_blob_service
from pipeline.run_lifecycle.pipeline_env import load_pipeline_env
from pipeline.provider_pipeline.provider_pipeline_orchestrator import ProviderPipelineOrchestrator
from pipeline.run_lifecycle.step_context import PipelineArgs
from pipeline.run_lifecycle.controller_release import (
    empty_pipeline_staging,
    fatal_on_worker_log_db_reports,
    fire_farewell_vm_delete,
    kill_active_workers,
    pause_pipeline_cluster,
    quiesce_mongo_state,
)

_log = ChatHealthyLoggingService()


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


def _control(ns):
    """Drive the controller and report."""
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

    from pipeline.run_lifecycle.pipeline_config import load_pipeline_config
    orchestrator = ProviderPipelineOrchestrator(
        env=ns.env_prefix,
        config=load_pipeline_config(env_prefix=ns.env_prefix),
        mongo_client=ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyDataPipelines"),
        blob_client=get_blob_service(),
    )
    if ns.run_id:
        os.environ["RUN_ID"] = ns.run_id
    _log.LogPipeline("INFO", 
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
                    "pipelineEditor", "ChatHealthyFrontEnd"
                )
                now = datetime.datetime.utcnow()
                # The run's own record names the process running it, so the
                # set of processes a run owns is knowable from the metadata
                # alone. Without it a reader had to recover the run id from
                # /proc/<pid>/environ on the host, which is only possible
                # while the host still answers.
                m["pipelineAdmin"]["pipeline.runs"].update_one(
                    {"run_id": rid},
                    {"$set": {"controller_heartbeat_at": now,
                              "controller_pid": os.getpid(),
                              "vm_name": os.environ.get("AZURE_VM_NAME", "")}},
                )
                # Extend reservation expiry_at so a long-running step past
                # the initial 10h TTL is not reaped by reservation_reaper.
                # Reaper reads expiry_at (post-fix); Watchdog reads
                # controller_heartbeat_at on the manifest.
                m["pipelineAdmin"]["cluster_lifecycle"].update_one(
                    {"_id": rid},
                    {"$set": {
                        "expiry_at": now + datetime.timedelta(hours=_RENEWAL_HOURS),
                        # What the reaper asks Azure about. expiry_at says only
                        # that nobody has waited long enough yet; this names the
                        # process whose absence ends the run now.
                        "controller_pid": os.getpid(),
                    }},
                )
            except Exception as exc:
                _log.LogPipeline("WARNING", "controller heartbeat write failed run_id=%s err=%s",
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
        _log.LogPipeline("INFO", "control_runner: run %s finished status=%s",
                  manifest.run_id if manifest else "(none)", final_status)
        exit_code = 0 if final_status == "succeeded" else 1
    except KeyboardInterrupt:
        # SIGINT, which is how a run is stopped on purpose. Naming the cause
        # is the whole point: without this the discrepancy report said
        # "failed" with fatal_exception None, so a deliberate stop and a
        # crash were indistinguishable in the record.
        final_status = "failed"
        _log.LogPipeline("ERROR", "control_runner: operator stop received; quiescing",
                   exc=ChatHealthyException(
                       mode="operator_stop",
                       message="Run stopped by operator signal (SIGINT); "
                               "quiescing.",
                       component="ControlRunner",
                   ))
        fatal_exception = ChatHealthyException(
            mode="operator_stop",
            message="Run stopped by operator signal (SIGINT); quiescing.",
            component="ControlRunner",
        )
    except ChatHealthyException as ch_exc:
        fatal_exception = ch_exc
        final_status = "failed"
        _log.LogPipeline("ERROR", "control_runner: fatal exception during orchestration: %s", ch_exc, exc=ch_exc)
    except Exception as other_exc:
        fatal_exception = other_exc
        final_status = "failed"
        _log.LogPipeline("ERROR", 
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
        # Workers never declare a run fatal; they state what happened to their
        # own work item and stop. The Controller reads those reports and is the
        # one that calls it -- run before the kill so the reason is recorded
        # while the evidence is still there. On any non-success terminal state,
        # kill every child process the Controller spawned so no worker keeps
        # writing after the run has abended (operator directive 2026-08-03).
        run_id_now = (
            (manifest.run_id if manifest and manifest.run_id else None)
            or os.environ.get("RUN_ID", "")
        )
        fatal_on_worker_log_db_reports(run_id_now)
        if final_status != "succeeded":
            kill_active_workers()
        quiesce_mongo_state(run_id_now, final_status)
        # Provider-specific, and it reads the NPPES staging collection, so it
        # runs here (not in the generic release) and before staging is emptied.
        _emit_discrepancy_report(run_id_now, final_status,
                                 manifest=manifest, args=args,
                                 fatal_exception=fatal_exception)
        # A successful run leaves PublicStaging empty; this also clears scratch
        # left behind by prior failed runs. Success only -- a failed run keeps
        # its staging for diagnosis. After the report's staging read, before
        # the cluster pause (a paused cluster cannot be dropped against).
        if final_status == "succeeded":
            empty_pipeline_staging()
        if manifest:
            pause_pipeline_cluster()
            fire_farewell_vm_delete()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv if argv is not None else sys.argv[1:])

    return _control(ns)


def _emit_discrepancy_report(run_id, final_status, *, manifest=None, args=None,
                             fatal_exception=None) -> None:
    """Emit the discrepancy report -- ALWAYS, perfect run OR abend (operator
    directive 2026-08-03: "we always in every case, even with a perfect job or
    an abend get the discrepancy report"). Provider-specific: source rows are
    the NPPES staging collection and the target is the published provider
    collection -- the next pipeline's numbers come from somewhere else entirely
    -- so this stays with the provider Controller, not in the generic release.
    Best-effort so a mongo/SparkPost outage doesn't block the run's exit."""
    try:
        pipeline_mongo = None
        try:
            pipeline_mongo = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")
        except Exception as mongo_exc:
            _log.LogPipeline("ERROR", "quiesce: mongo unreachable for discrepancy report run_id=%s err=%s",
                       run_id, str(mongo_exc)[:500])
            fatal_exception = fatal_exception or mongo_exc

        if pipeline_mongo:
            # Mongo is reachable - use normal emit_discrepancy_report path
            from chathealthy_lib.discrepancy_report import emit_discrepancy_report  # noqa: PLC0415
            from pipeline.run_lifecycle.pipeline_config import load_pipeline_config  # noqa: PLC0415
            env_prefix = os.environ.get("ENV_PREFIX", "dev")
            cfg = load_pipeline_config(mongo_client=None, env_prefix=env_prefix)
            manifest_status = manifest.status if manifest else final_status
            manifest_doc = (
                manifest.to_document()
                if manifest and hasattr(manifest, "to_document") else
                {"run_id": run_id, "status": manifest_status}
            )
            # A failed run whose exception did not reach here still knows what
            # went wrong: the worker wrote it to pipeline.work_items before it
            # died. Without this the report said "fatal error reported without
            # an exception" -- a statement about the report, not about the run
            # -- next to a row count of zero it never explained. Now it names
            # the step, the exception type and the message.
            if fatal_exception is None and manifest_status not in ("succeeded", "completed"):
                try:
                    failed = ChatHealthyMongoUtilities().getConnection(
                        "pipelineEditor", "ChatHealthyFrontEnd"
                    )["pipelineAdmin"]["pipeline.work_items"].find_one(
                        {"run_id": run_id, "status": {"$in": ["failed", "error"]}},
                        sort=[("finished_at", 1)],
                    ) or {}
                    err = ((failed.get("output") or {}).get("error")
                           or failed.get("error") or {})
                    if err:
                        part = (failed.get("payload") or {}).get("partition") or {}
                        where = failed.get("step", "unknown step")
                        if part:
                            where += f" {part}"
                        fatal_exception = ChatHealthyException(
                            mode="pipeline_step_failed",
                            component="control_runner",
                            message=(f"{where} failed with "
                                     f"{err.get('type', 'error')}: {err.get('msg', '')}"),
                            step=failed.get("step"),
                        )
                except Exception as lookup_exc:  # noqa: BLE001
                    _log.LogPipeline("WARNING", "quiesce: could not read the failing work item "
                                 "run_id=%s (%s)", run_id, str(lookup_exc)[:160])
            if fatal_exception:
                manifest_doc["fatal_exception"] = {
                    "type": type(fatal_exception).__name__,
                    "message": str(fatal_exception),
                    "mode": getattr(fatal_exception, "mode", "unknown"),
                }
            # Which two collections the report counts. The registry owns the
            # source-to-collection map, so the names are asked for, never
            # built here out of an env var. A failure to resolve is said out
            # loud and the counts read Unknown; it is not papered over with a
            # constructed name that may address nothing.
            # The three row counts are this pipeline's to take, not the
            # report's to go looking for. Only here is it known that source
            # rows mean the NPPES staging collection and that the target is
            # the published provider collection; the next pipeline's three
            # numbers will come from somewhere else entirely.
            #
            # They are counted against the DATA cluster. pipeline_mongo above
            # reaches the metadata cluster, where the run's own records live.
            # Counting provider data through it returns 0 -- not an error and
            # not Unknown, just a confident wrong number.
            #
            # Counted here, at quiesce, because the run is over: a count taken
            # earlier and carried down is a count from whenever it was taken.
            # A count that cannot be taken stays None and reads Unknown.
            target_collection = ""
            total_source_rows = rows_in_target = total_rows = None
            try:
                from pipeline.run_lifecycle.pipeline_dataset_registry import PipelineDatasetRegistry  # noqa: PLC0415
                registry = PipelineDatasetRegistry(
                    cfg, int(args.data_version), pipeline_mongo)
                target_entry = registry.by_source_name("provider")
                source_entry = registry.by_source_name("nppes_npi")
                target_collection = (
                    f"{target_entry.public_data_db}."
                    f"{registry.public_data_collection_name('provider')}")
                source_collection = (
                    f"{source_entry.staging_db}."
                    f"{registry.staging_collection_name('nppes_npi')}")
                data_mongo = ChatHealthyMongoUtilities().getConnection(
                    "pipelineEditor", "ChatHealthyDataPipelines")
                t_db, t_coll = target_collection.split(".", 1)
                s_db, s_coll = source_collection.split(".", 1)
                target = data_mongo[t_db][t_coll]
                rows_in_target = target.count_documents({"run_id": run_id})
                total_rows = target.count_documents({})
                total_source_rows = data_mongo[s_db][s_coll].count_documents({})
            except Exception as exc:  # noqa: BLE001
                _log.LogPipeline("ERROR", "quiesce: the row counts could not be taken "
                           "run_id=%s err=%s; the report will say Unknown",
                           run_id, str(exc)[:300])

            if not target_collection:
                # Resolve straight from config so a hiccup above never loses the
                # whole report (operator: always get the report).
                _dver = os.environ.get("DATA_VERSION", "").strip()
                if not _dver.isdigit() and args is not None:
                    _dver = str(getattr(args, "data_version", "") or "")
                for _dv in (cfg.get("dataset_versions") or []):
                    if _dv.get("source_name") == "provider":
                        _pdn = _dv.get("public_data_name") or ""
                        if "." in _pdn and _dver.isdigit():
                            target_collection = f"{_pdn}_v_{_dver}"
                        break
                if target_collection:
                    _log.LogPipeline("WARNING", "quiesce: target_collection resolved from "
                               "config fallback run_id=%s -> %s", run_id, target_collection)

            summary = emit_discrepancy_report(
                pipeline_mongo=pipeline_mongo,
                run_id=run_id,
                manifest_status=manifest_status,
                manifest_doc=manifest_doc,
                config=cfg,
                target_collection=target_collection,
                total_source_rows=total_source_rows,
                rows_in_target=rows_in_target,
                total_rows=total_rows,
                operator_email=getattr(args, "operator_email", None) if args else None,
                operator_sms=getattr(args, "operator_sms", None) if args else None,
            )
            _log.LogPipeline("INFO", 
                "quiesce: discrepancy report emitted run_id=%s total=%d pdf_bytes=%d",
                run_id, summary.get("total", 0), summary.get("pdf_bytes", 0),
            )
        else:
            # Mongo unreachable - emit minimal report to stderr
            _log.LogPipeline("ERROR", 
                "quiesce: DISCREPANCY REPORT run_id=%s status=%s fatal_exception=%s",
                run_id, final_status,
                f"{type(fatal_exception).__name__}: {fatal_exception}" if fatal_exception else "None"
            )
    except Exception as exc:
        _log.LogPipeline("ERROR", "quiesce: discrepancy report FAILED run_id=%s err=%s",
                   run_id, str(exc)[:500])


if __name__ == "__main__":
    sys.exit(main())
