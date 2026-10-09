# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Medicare Provider Evaluation Data Pipeline (EPIC-010-F-007) — Controller entry.

Mirrors the provider pipeline's control_runner: runs inside the pipeline image
on the Pipeline Run VM, reads/updates the run manifest, walks the StepSpec DAG
spawning Workers, and quiesces on any terminal state through the generic
run-end release (pipeline.run_lifecycle.controller_release).

Medicare-specific invocation: --build-indication-map gates the rebuild of the
Normalized Indication Map (default off -> consume the current published map).
The flag is exported as BUILD_INDICATION_MAP so Worker subprocesses (which
inherit os.environ) and the build_indication_map step see it.

The finally emits exactly one Medicare discrepancy report per run, on success
and abend alike (EPIC-010-F-001-S-008), counting the three published Medicare
collections.
"""

from __future__ import annotations

import os

# CH_LOG_DESTINATION must be set before the first ChatHealthyLoggingService call.
os.environ.setdefault("CH_LOG_DESTINATION", "stderr,mongo")
os.environ.setdefault("CH_SPACE_NAME", "controller")
os.environ.setdefault("CH_COMPONENT", "medicare_pipeline_control")

from chathealthy_lib.logging_service import (  # noqa: E402
    ChatHealthyLoggingService, set_mongo_log_identity)
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

set_mongo_log_identity("pipelineEditor")

import argparse  # noqa: E402
import datetime  # noqa: E402
import sys  # noqa: E402

from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

from pipeline.run_lifecycle.blob_client import get_blob_service  # noqa: E402
from pipeline.run_lifecycle.pipeline_env import load_pipeline_env  # noqa: E402
from pipeline.run_lifecycle.pipeline_config import load_pipeline_config  # noqa: E402
from pipeline.run_lifecycle.step_context import PipelineArgs  # noqa: E402
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_orchestrator import (  # noqa: E402
    MedicareProviderEvaluationOrchestrator)
from pipeline.run_lifecycle.controller_release import (  # noqa: E402
    empty_pipeline_staging,
    fatal_on_worker_log_db_reports,
    fire_farewell_vm_delete,
    kill_active_workers,
    pause_pipeline_cluster,
    quiesce_mongo_state,
)
from pipeline.run_lifecycle import controller_phase as _phase  # noqa: E402

_log = ChatHealthyLoggingService()


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Medicare Provider Evaluation pipeline Control runner")
    parser.add_argument("--run-id", dest="run_id",
                        default=os.environ.get("RUN_ID") or None,
                        help="Existing manifest run_id; a new one is minted if omitted")
    parser.add_argument("--env-prefix", dest="env_prefix",
                        default=os.environ.get("ENV_PREFIX", "dev"),
                        help="Environment prefix (local|dev|qa|prod)")
    parser.add_argument("--states", dest="states",
                        default=os.environ.get("STATES", "ALL"),
                        help="Comma-separated state list or ALL (provider fan-out scope)")
    parser.add_argument("--resume-from-step", dest="resume_from_step",
                        default=os.environ.get("RESUME_FROM_STEP") or None,
                        help="Skip completed steps up to this step name")
    parser.add_argument("--expected-duration-minutes", dest="expected_duration_minutes",
                        type=int,
                        default=int(os.environ.get("EXPECTED_DURATION_MINUTES", "120")))
    parser.add_argument("--log-level", dest="log_level",
                        default=os.environ.get("LOG_LEVEL", "INFO"))
    _dv_env = os.environ.get("DATA_VERSION", "").strip()
    _dv_default = int(_dv_env) if _dv_env.isdigit() else None
    parser.add_argument("--data-version", dest="data_version", type=int,
                        default=_dv_default, required=(_dv_default is None),
                        help="Output collection version number. MANDATORY. "
                             "Reads DATA_VERSION env if the flag is omitted.")
    # Gate the rebuild of the Normalized Indication Map. Off by default: a
    # normal run consumes the current published map. On via
    # --build-indication-map or BUILD_INDICATION_MAP env in {1,true,yes}.
    _bim_default = _truthy(os.environ.get("BUILD_INDICATION_MAP", ""))
    parser.add_argument("--build-indication-map", dest="build_indication_map",
                        action="store_true", default=_bim_default,
                        help="Rebuild the Normalized Indication Map this run "
                             "(produces a candidate for operator-gated release). "
                             "Default off: consume the current published map.")
    return parser.parse_args(argv)


def _states_list(raw: str) -> list[str]:
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _control(ns) -> int:
    if ns.log_level:
        os.environ.setdefault("LOG_LEVEL", ns.log_level.upper())

    load_pipeline_env()

    os.environ["DATA_VERSION"] = str(ns.data_version)
    os.environ["BUILD_INDICATION_MAP"] = "1" if ns.build_indication_map else "0"
    # The shared discrepancy-report emitter labels the report by PIPELINE_NAME;
    # name it here so the Medicare report is not emitted under the provider name.
    os.environ["PIPELINE_NAME"] = MedicareProviderEvaluationOrchestrator.PIPELINE_NAME
    # The human-facing report title/subject uses the display name; the key
    # "medicare" stays the config/run-record lookup key and is unchanged.
    os.environ["PIPELINE_DISPLAY_NAME"] = (
        MedicareProviderEvaluationOrchestrator.PIPELINE_DISPLAY_NAME
        or MedicareProviderEvaluationOrchestrator.PIPELINE_NAME)
    if ns.run_id:
        os.environ["RUN_ID"] = ns.run_id

    _phase.set_controller_phase(os.environ.get("RUN_ID", ""),
                                _phase.CONTROLLER_STARTING,
                                "controller process up; starting status listener")

    # Run-status listener FIRST (EPIC-010-F-001-S-015): the controller answers
    # the status call on :6969 (mTLS) within seconds of starting, reading the
    # always-on front-end cluster -- independent of the pipeline-cluster wake
    # and the orchestrator build below. Step names come from the class, so the
    # listener needs no orchestrator instance. A bind failure is observability
    # lost, not the work lost, so it is logged and the run proceeds.
    try:
        from pipeline.run_lifecycle.run_status_server import start_status_server  # noqa: PLC0415
        start_status_server([s.name for s in MedicareProviderEvaluationOrchestrator.STEPS])
        _phase.set_controller_phase(os.environ.get("RUN_ID", ""), _phase.STATUS_API_UP,
                                    "mTLS status API bound on :6969")
    except Exception as exc:  # noqa: BLE001
        _log.LogPipeline("WARNING",
                         "medicare control_runner: status listener failed to start: %s",
                         str(exc)[:200])

    # Controller heartbeat: writes controller_heartbeat_at every 60s so the
    # Watchdog can tell a live run from an abandoned one. Reads the always-on
    # front-end cluster, so it runs during the pipeline-cluster wake. Daemon
    # thread dies with the process.
    import threading  # noqa: PLC0415
    _hb_stop = threading.Event()
    _RENEWAL_HOURS = 2

    def _heartbeat() -> None:
        rid = ns.run_id or os.environ.get("RUN_ID", "")
        if not rid:
            return
        while not _hb_stop.wait(60):
            try:
                m = ChatHealthyMongoUtilities().getConnection(
                    "pipelineEditor", "ChatHealthyFrontEnd")
                now = datetime.datetime.utcnow()
                m["pipelineAdmin"]["pipeline.runs"].update_one(
                    {"run_id": rid},
                    {"$set": {"controller_heartbeat_at": now,
                              "controller_pid": os.getpid(),
                              "vm_name": os.environ.get("AZURE_VM_NAME", "")}},
                )
                m["pipelineAdmin"]["cluster_lifecycle"].update_one(
                    {"_id": rid},
                    {"$set": {
                        "expiry_at": now + datetime.timedelta(hours=_RENEWAL_HOURS),
                        "controller_pid": os.getpid(),
                    }},
                )
            except Exception as exc:  # noqa: BLE001
                _log.LogPipeline("WARNING",
                                 "medicare controller heartbeat write failed run_id=%s err=%s",
                                 rid, str(exc)[:200])

    threading.Thread(target=_heartbeat, daemon=True, name="controller-heartbeat").start()

    # Worker-box verifier: once orchestration is driving the two-VM path, the
    # Controller LOOKS at the Worker box (ARM runCommand -> docker exec pgrep) and
    # records what it sees. The run is 'fan_out_running' only when the pool PIDs
    # are seen there -- box up is not process up. Only runs on the VM path
    # (WORKER_COMPUTE=vm); a local/subprocess run leaves it idle. Daemon thread.
    _verify_stop = threading.Event()

    def _verify_workers() -> None:
        rid = ns.run_id or os.environ.get("RUN_ID", "")
        if not rid or os.environ.get("WORKER_COMPUTE", "").lower() != "vm":
            return
        from pipeline.run_lifecycle.worker_pid_verifier import probe_worker_box  # noqa: PLC0415
        interval = 45
        while not _verify_stop.wait(interval):
            r = probe_worker_box(rid)
            if r.get("verified"):
                _phase.set_worker_phase(rid, _phase.WORKER_FAN_OUT_RUNNING,
                                        pool=r.get("pool", 0), host=r.get("host", 0),
                                        detail=r.get("detail", ""))
                interval = 180  # fan-out confirmed; slow the probe
            elif not r.get("container"):
                _phase.set_worker_phase(rid, _phase.WORKER_NO_BOX,
                                        detail=r.get("detail", ""))
            elif r.get("host", 0) >= 1:
                _phase.set_worker_phase(rid, _phase.WORKER_HOST_UP_NO_FANOUT,
                                        host=r.get("host", 0), detail=r.get("detail", ""))
            else:
                _phase.set_worker_phase(rid, _phase.WORKER_BOX_PROVISIONING,
                                        detail=r.get("detail", ""))

    threading.Thread(target=_verify_workers, daemon=True, name="worker-verifier").start()

    manifest = None
    final_status = "failed"
    exit_code = 1
    fatal_exception = None
    # Bound before the try so the finally's discrepancy report can read it even
    # when a wake or build failure stops the run before args is assigned below.
    args = None

    try:
        # The trigger runbook wakes the pipeline cluster and waits for IDLE
        # before it provisions this Controller VM, so the Controller does not
        # re-wake it (the provider controller does not either). The former
        # re-wake read os.environ["PIPELINE_CLUSTER"] -- a var the controller
        # cloud-init never sets -- and crashed the run on a KeyError before any
        # step ran (ebcffb35). The cluster is already IDLE here. Inside the try
        # so a build failure still runs the quiesce below.
        _phase.set_controller_phase(os.environ.get("RUN_ID", ""), _phase.CLUSTER_READY,
                                    "pipeline cluster woken by the runbook; building orchestrator")

        args = PipelineArgs(
            states=_states_list(ns.states),
            env_prefix=ns.env_prefix,
            expected_duration_minutes=ns.expected_duration_minutes,
            resume_from_step=ns.resume_from_step,
            run_id=ns.run_id,
            data_version=ns.data_version,
        )
        orchestrator = MedicareProviderEvaluationOrchestrator(
            env=ns.env_prefix,
            config=load_pipeline_config(env_prefix=ns.env_prefix),
            mongo_client=ChatHealthyMongoUtilities().getConnection(
                "pipelineEditor", "ChatHealthyDataPipelines"),
            blob_client=get_blob_service(),
        )
        _log.LogPipeline(
            "INFO",
            "medicare control_runner: starting run env_prefix=%s states=%s "
            "resume_from_step=%s data_version=%s build_indication_map=%s run_id=%s",
            ns.env_prefix, args.states, ns.resume_from_step, ns.data_version,
            ns.build_indication_map, ns.run_id or "(mint)",
        )
        _phase.set_controller_phase(os.environ.get("RUN_ID", ""), _phase.ORCHESTRATING,
                                    "walking the step DAG; fanning out work_items")
        manifest = orchestrator.run(args)
        if manifest and manifest.run_id:
            os.environ["RUN_ID"] = manifest.run_id
        final_status = manifest.status if manifest else "failed"
        _log.LogPipeline("INFO", "medicare control_runner: run %s finished status=%s",
                         manifest.run_id if manifest else "(none)", final_status)
        exit_code = 0 if final_status == "succeeded" else 1
    except KeyboardInterrupt:
        final_status = "failed"
        fatal_exception = ChatHealthyException(
            mode="operator_stop",
            message="Run stopped by operator signal (SIGINT); quiescing.",
            component="MedicareControlRunner",
        )
        _log.LogPipeline("ERROR", "medicare control_runner: operator stop received; quiescing",
                         exc=ChatHealthyException(
                             mode="operator_stop",
                             message="Run stopped by operator signal (SIGINT); quiescing.",
                             component="MedicareControlRunner"))
    except ChatHealthyException as ch_exc:
        fatal_exception = ch_exc
        final_status = "failed"
        _log.LogPipeline("ERROR",
                         "medicare control_runner: fatal exception during orchestration: %s",
                         ch_exc, exc=ch_exc)
    except Exception as other_exc:  # noqa: BLE001
        final_status = "failed"
        fatal_exception = ChatHealthyException(
            mode="orchestration_failure",
            message=f"Fatal exception during orchestration: {other_exc}",
            component="MedicareControlRunner",
            exception=other_exc,
        )
        _log.LogPipeline("ERROR",
                         "medicare control_runner: fatal exception during orchestration: %s",
                         other_exc, exc=ChatHealthyException(
                             mode="orchestration_failure",
                             message=f"Fatal exception during orchestration: {other_exc}",
                             component="MedicareControlRunner",
                             exception=other_exc))
    finally:
        run_id_now = (
            (manifest.run_id if manifest and manifest.run_id else None)
            or os.environ.get("RUN_ID", "")
        )
        _verify_stop.set()
        _hb_stop.set()
        _phase.set_controller_phase(run_id_now, _phase.QUIESCING,
                                    f"run ending status={final_status}; releasing")
        fatal_on_worker_log_db_reports(run_id_now)
        if final_status != "succeeded":
            kill_active_workers()
        quiesce_mongo_state(run_id_now, final_status)
        # The Medicare discrepancy report -- one per run on success AND abend
        # (EPIC-010-F-001-S-008) -- counts the three published Medicare
        # collections, so the Medicare Controller emits it here, before staging
        # is emptied and the cluster is paused.
        _emit_discrepancy_report(run_id_now, final_status,
                                 manifest=manifest, args=args,
                                 fatal_exception=fatal_exception)
        if final_status == "succeeded":
            empty_pipeline_staging()
        if manifest:
            pause_pipeline_cluster()
            fire_farewell_vm_delete()
        _phase.set_controller_phase(run_id_now, _phase.RELEASED,
                                    f"run released; final status={final_status}")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv if argv is not None else sys.argv[1:])
    return _control(ns)


def _derive_step_status_reason(pipeline_mongo, run_id):
    """The guaranteed-minimum failure reason, derived from the run's own step
    status. Called only when no worker wrote a per-partition error and no
    exception reached the controller's finally: a failed run must never be
    recorded with no reason (EPIC-010-F-001-S-008). Reads pipeline.work_items,
    groups by step, and names the step the orchestrator stopped at plus the
    failed/total partition tally. Returns a constructed (not raised)
    ChatHealthyException, or None if nothing could be read. Best-effort: a read
    failure here must never block the report's delivery, so it is logged and
    None is returned."""
    try:
        items = list(pipeline_mongo["pipelineAdmin"]["pipeline.work_items"].find(
            {"run_id": run_id}, {"step": 1, "status": 1}))
    except Exception as read_exc:  # noqa: BLE001
        _log.LogPipeline("WARNING", "medicare quiesce: could not read work-item step "
                         "status for the minimum reason run_id=%s (%s)",
                         run_id, str(read_exc)[:160])
        return None

    done_states = ("done", "completed", "succeeded")
    failed_states = ("failed", "error")
    by_step: dict = {}
    for it in items:
        tally = by_step.setdefault(it.get("step") or "unknown",
                                   {"total": 0, "failed": 0, "done": 0})
        tally["total"] += 1
        status = str(it.get("status") or "").lower()
        if status in failed_states:
            tally["failed"] += 1
        elif status in done_states:
            tally["done"] += 1

    if not by_step:
        return ChatHealthyException(
            mode="pipeline_step_failed",
            component="MedicareControlRunner",
            message=("run ended in status failed before any work item was "
                     "enqueued; no step-level status to derive a reason from"))

    failed_steps = {s: t for s, t in by_step.items() if t["failed"] > 0}
    if failed_steps:
        step = next(iter(failed_steps))
        t = failed_steps[step]
        return ChatHealthyException(
            mode="pipeline_step_failed",
            component="MedicareControlRunner",
            message=(f"run did not proceed past step {step} "
                     f"({t['failed']}/{t['total']} partitions failed; no "
                     f"worker-level error captured)"),
            step=step)

    incomplete = {s: t for s, t in by_step.items() if t["done"] < t["total"]}
    if incomplete:
        step = next(iter(incomplete))
        t = incomplete[step]
        return ChatHealthyException(
            mode="pipeline_step_failed",
            component="MedicareControlRunner",
            message=(f"run did not complete step {step} "
                     f"({t['done']}/{t['total']} partitions done; no terminal "
                     f"worker status and no worker-level error captured)"),
            step=step)

    return ChatHealthyException(
        mode="pipeline_step_failed",
        component="MedicareControlRunner",
        message=("run ended in status failed though every work item reached a "
                 "done state and no worker-level error was captured"))


def _emit_discrepancy_report(run_id, final_status, *, manifest=None, args=None,
                             fatal_exception=None) -> None:
    """Emit the Medicare discrepancy report -- ALWAYS, on a perfect run OR an
    abend (EPIC-010-F-001-S-008). Medicare-specific: it counts the three
    published collections this pipeline builds -- ProviderMedicare,
    SpecialtyMedicare and the Normalized Indication Map -- so it stays with the
    Medicare Controller, not the generic release. Best-effort so a Mongo or
    SparkPost outage never blocks the run's exit, and delivered even when the
    run-record store is unreachable (EPIC-010-F-001-S-008-REQ-B-006)."""
    try:
        pipeline_mongo = None
        try:
            pipeline_mongo = ChatHealthyMongoUtilities().getConnection(
                "pipelineEditor", "ChatHealthyFrontEnd")
        except Exception as mongo_exc:  # noqa: BLE001
            _log.LogPipeline("ERROR", "medicare quiesce: mongo unreachable for discrepancy "
                             "report run_id=%s err=%s", run_id, str(mongo_exc)[:500])
            fatal_exception = fatal_exception or mongo_exc

        if not pipeline_mongo:
            # The store is down but the report is still owed: name the run's end
            # on stderr so a failed run is never silent.
            _log.LogPipeline("ERROR", "medicare quiesce: DISCREPANCY REPORT run_id=%s "
                             "status=%s fatal_exception=%s", run_id, final_status,
                             (f"{type(fatal_exception).__name__}: {fatal_exception}"
                              if fatal_exception else "None"))
            return

        from chathealthy_lib.discrepancy_report import (  # noqa: PLC0415
            emit_discrepancy_report_for_collections)
        env_prefix = os.environ.get("ENV_PREFIX", "dev")
        cfg = load_pipeline_config(env_prefix=env_prefix)
        manifest_status = manifest.status if manifest else final_status
        manifest_doc = (
            manifest.to_document()
            if manifest and hasattr(manifest, "to_document")
            else {"run_id": run_id, "status": manifest_status}
        )

        # A failed run whose exception did not reach the finally still knows what
        # went wrong: the worker wrote it to pipeline.work_items before it died.
        # Name the step, the exception type and the message so the report states
        # what failed, not merely that something did.
        if fatal_exception is None and manifest_status not in ("succeeded", "completed"):
            try:
                failed = pipeline_mongo["pipelineAdmin"]["pipeline.work_items"].find_one(
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
                        component="MedicareControlRunner",
                        message=(f"{where} failed with "
                                 f"{err.get('type', 'error')}: {err.get('msg', '')}"),
                        step=failed.get("step"),
                    )
            except Exception as lookup_exc:  # noqa: BLE001
                _log.LogPipeline("WARNING", "medicare quiesce: could not read the failing "
                                 "work item run_id=%s (%s)", run_id, str(lookup_exc)[:160])
        # (3) Guaranteed-minimum reason. When no worker wrote a per-partition
        # error (1) and no exception reached the finally (2), the run still
        # knows which step the orchestrator marked failed and the tally of
        # failed partitions. Derive that from the run's own work-item step
        # status so a failed run is NEVER recorded without a reason
        # (EPIC-010-F-001-S-008). The step-level statement IS the true minimum.
        if fatal_exception is None and manifest_status not in ("succeeded", "completed"):
            fatal_exception = _derive_step_status_reason(pipeline_mongo, run_id)
        if fatal_exception:
            manifest_doc["fatal_exception"] = {
                "type": type(fatal_exception).__name__,
                "message": str(fatal_exception),
                "mode": getattr(fatal_exception, "mode", "unknown"),
            }

        # Count the three published Medicare collections against the DATA
        # cluster -- pipeline_mongo above reaches the metadata cluster, where the
        # run records live, and a data count through it returns a confident zero.
        # The registry owns the source-to-collection map, so the names are asked
        # for, never built from an env var. ProviderMedicare is first, so it is
        # the report's primary (one document per NPI); every collection appears
        # in the report. A count that cannot be taken stays None and reads
        # Unknown.
        dv = os.environ.get("DATA_VERSION", "").strip()
        collections: list[dict] = []
        try:
            from pipeline.run_lifecycle.pipeline_dataset_registry import PipelineDatasetRegistry  # noqa: PLC0415
            data_version = int(dv) if dv.isdigit() else int(getattr(args, "data_version", 0) or 0)
            registry = PipelineDatasetRegistry(cfg, data_version, pipeline_mongo)
            data_mongo = ChatHealthyMongoUtilities().getConnection(
                "pipelineEditor", "ChatHealthyDataPipelines")
            for source_name in ("providermedicare", "specialtymedicare", "indication_map"):
                entry = registry.by_source_name(source_name)
                coll_name = registry.public_data_collection_name(source_name)
                coll = data_mongo[entry.public_data_db][coll_name]
                collections.append({
                    "collection": f"{entry.public_data_db}.{coll_name}",
                    "rows_in_target": coll.count_documents({"run_id": run_id}),
                    "total_rows": coll.count_documents({}),
                })
            _log.LogPipeline("INFO", "medicare quiesce: collection counts run_id=%s %s",
                             run_id,
                             {c["collection"].split(".")[-1]:
                              (c["rows_in_target"], c["total_rows"]) for c in collections})
        except Exception as exc:  # noqa: BLE001
            _log.LogPipeline("ERROR", "medicare quiesce: the collection counts could not be "
                             "taken run_id=%s err=%s; the report will say Unknown",
                             run_id, str(exc)[:300])

        if not collections:
            # Resolve the primary straight from config so a hiccup above never
            # loses the whole report -- the report is owed on every run.
            for _dv in (cfg.get("dataset_versions") or []):
                if _dv.get("source_name") == "providermedicare":
                    _pdn = _dv.get("public_data_name") or ""
                    if "." in _pdn and dv.isdigit():
                        collections = [{"collection": f"{_pdn}_v_{dv}",
                                        "rows_in_target": None, "total_rows": None}]
                    break
            if collections:
                _log.LogPipeline("WARNING", "medicare quiesce: collections resolved from "
                                 "config fallback run_id=%s -> %s",
                                 run_id, collections[0]["collection"])

        if not collections:
            # The report is owed on EVERY run (EPIC-010-F-001-S-008), including a
            # run that failed before any collection could be resolved. Synthesize
            # a valid target from the known public db + this run's data_version so
            # the report STILL delivers, with Unknown counts -- never silently
            # dropped by the emitter's target validator (the bug that ate it).
            _dv_s = dv if dv.isdigit() else "unknown"
            collections = [{"collection": f"PipelinePublicHealthData.ProviderMedicare_v_{_dv_s}",
                            "rows_in_target": None, "total_rows": None}]
            _log.LogPipeline("WARNING", "medicare quiesce: no collection resolved; report "
                             "sends with Unknown counts target=%s run_id=%s",
                             collections[0]["collection"], run_id)

        summary = emit_discrepancy_report_for_collections(
            pipeline_mongo=pipeline_mongo,
            run_id=run_id,
            manifest_status=manifest_status,
            manifest_doc=manifest_doc,
            config=cfg,
            collections=collections,
            total_source_rows=None,
            operator_email=getattr(args, "operator_email", None) if args else None,
            operator_sms=getattr(args, "operator_sms", None) if args else None,
        )
        # Log what actually happened -- never report "emitted" when the emitter
        # sent nothing (the misleading line that hid this bug).
        if summary.get("email_sent"):
            _log.LogPipeline("INFO", "medicare quiesce: discrepancy report DELIVERED "
                             "run_id=%s total=%d", run_id, summary.get("total", 0))
        else:
            _log.LogPipeline("ERROR", "medicare quiesce: discrepancy report NOT DELIVERED "
                             "run_id=%s err=%s", run_id, summary.get("error", "unknown"))
    except Exception as exc:  # noqa: BLE001
        _log.LogPipeline("ERROR", "medicare quiesce: discrepancy report FAILED run_id=%s "
                         "err=%s", run_id, str(exc)[:500])


if __name__ == "__main__":
    sys.exit(main())
