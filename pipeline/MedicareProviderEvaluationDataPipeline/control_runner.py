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

Slice 1 scaffold: the step runners are stubs; the Medicare discrepancy report
is a later slice (flagged below).
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
    if ns.run_id:
        os.environ["RUN_ID"] = ns.run_id

    _log.LogPipeline(
        "INFO",
        "medicare control_runner: starting run env_prefix=%s states=%s "
        "resume_from_step=%s data_version=%s build_indication_map=%s run_id=%s",
        ns.env_prefix, args.states, ns.resume_from_step, ns.data_version,
        ns.build_indication_map, ns.run_id or "(mint)",
    )

    # Controller heartbeat: writes controller_heartbeat_at every 60s so the
    # Watchdog can tell a live run from an abandoned one. Daemon thread dies
    # with the process.
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

    # Run-status listener (EPIC-010-F-001-S-015): once established the controller
    # answers the status call on :6969 (mTLS) for the life of the run. A bind
    # failure is observability lost, not the work lost, so it is logged and the
    # run proceeds -- the client simply sees the API down.
    try:
        from pipeline.run_lifecycle.run_status_server import start_status_server  # noqa: PLC0415
        start_status_server([s.name for s in orchestrator.STEPS])
    except Exception as exc:  # noqa: BLE001
        _log.LogPipeline("WARNING",
                         "medicare control_runner: status listener failed to start: %s",
                         str(exc)[:200])

    manifest = None
    final_status = "failed"
    exit_code = 1
    fatal_exception = None

    try:
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
        fatal_on_worker_log_db_reports(run_id_now)
        if final_status != "succeeded":
            kill_active_workers()
        quiesce_mongo_state(run_id_now, final_status)
        # TODO (later slice): emit the Medicare discrepancy report here --
        # one per run on success or abend -- counting the three Medicare
        # collections, once their dataset_versions[] entries are declared.
        if final_status == "succeeded":
            empty_pipeline_staging()
        if manifest:
            pause_pipeline_cluster()
            fire_farewell_vm_delete()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv if argv is not None else sys.argv[1:])
    return _control(ns)


if __name__ == "__main__":
    sys.exit(main())
