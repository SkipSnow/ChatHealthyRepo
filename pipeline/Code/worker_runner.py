# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Provider Pipeline LLD v23 §2.3 — ACA Worker container entry point.

Invoked by bootstrap.py inside a per-stage ACA Job replica as
    python worker_runner.py --run-id ... --step ... --env-prefix ...
Environment variables set by aca_job_manager.start_job carry the run
context (RUN_ID, ENV_PREFIX, PIPELINE_STEP) plus PART_* variables that
name the partition the replica must process.

The worker resolves STEP_RUNNERS[step_name] and invokes it with a
StepContext whose config["partition"] carries the PART_* values.
"""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
from pipeline_fatal_recorder import is_log_db_fatal
from pipeline_db import PIPELINE_ADMIN_DB, get_frontend_mongo

# Distinct from a step failure: the logging substrate never came up.
EXIT_LOG_DB_FATAL = 78

import argparse
import os
import sys

from blob_client import get_blob_service
from pipeline_db import get_mongo
from pipeline_env import load_pipeline_env
from step_context import PipelineArgs, RunManifest, StepContext
from steps import get_runner

_log = ChatHealthyLoggingService()

_PARTITION_ENV_PREFIX = "PART_"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provider Pipeline Worker runner")
    parser.add_argument("--run-id", dest="run_id",
                        default=os.environ.get("RUN_ID"),
                        required=False)
    parser.add_argument("--step", dest="step",
                        default=os.environ.get("PIPELINE_STEP"),
                        required=False,
                        help="LLD step name; resolved via STEP_RUNNERS")
    parser.add_argument("--env-prefix", dest="env_prefix",
                        default=os.environ.get("ENV_PREFIX", "dev"))
    parser.add_argument("--states", dest="states",
                        default=os.environ.get("STATES", "ALL"))
    parser.add_argument("--log-level", dest="log_level",
                        default=os.environ.get("LOG_LEVEL", "INFO"))
    return parser.parse_args(argv)


def _partition_from_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in os.environ.items():
        if k.startswith(_PARTITION_ENV_PREFIX):
            out[k[len(_PARTITION_ENV_PREFIX):].lower()] = v
    return out


def _states_list(raw: str) -> list[str]:
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _raise_on_stop_signal(signum, _frame) -> None:
    """Turn a stop signal into an exception so the stack unwinds.

    SIGTERM has no default Python handler: the process dies where it stands
    and no finally runs, which is why every worker the Controller stopped left
    its work item claimed by a process that no longer existed. KeyboardInterrupt
    derives from BaseException, so it passes through this module's
    except-ChatHealthyException and except-Exception clauses untouched and
    reaches the finally that reports the work item.
    """
    raise KeyboardInterrupt(f"stop signal {signum}")


def main(argv: list[str] | None = None) -> int:
    import signal  # noqa: PLC0415
    signal.signal(signal.SIGTERM, _raise_on_stop_signal)
    signal.signal(signal.SIGINT, _raise_on_stop_signal)

    ns = _parse_args(argv if argv is not None else sys.argv[1:])

    if ns.log_level:
        os.environ.setdefault("LOG_LEVEL", ns.log_level.upper())

    if not ns.step:
        raise ChatHealthyException(mode="value_error", message="worker_runner: --step or PIPELINE_STEP env var required")

    # Every exit path reports to the Controller. A Worker NEVER declares a run
    # fatal -- it states what happened to its own work item and stops. The
    # Controller reads these and decides. The report is in a finally because
    # the failure that matters most (the logging substrate refusing to start)
    # can happen before the step ever runs, and a silently dead subprocess is
    # the one outcome the Controller cannot act on.
    status, reason, detail = "failed", "worker_died_without_reporting", ""
    try:
        load_pipeline_env()

        args = PipelineArgs(
            states=_states_list(ns.states),
            env_prefix=ns.env_prefix,
            run_id=ns.run_id,
        )
        manifest = RunManifest.new("provider", args)
        partition = _partition_from_env()

        ctx = StepContext(
            args=args,
            manifest=manifest,
            config={"partition": partition},
            mongo_client=get_mongo("ChatHealthyDataPipelines"),
            blob_client=get_blob_service(),
        )
        runner = get_runner(ns.step)
        result = runner(ctx)
        status, reason, detail = "succeeded", "", ""
        return 0
    except KeyboardInterrupt as exc:
        status, reason, detail = "failed", "stopped_by_controller", str(exc)[:500]
        raise
    except ChatHealthyException as exc:
        reason = f"log_db_fatal:{exc.mode}" if is_log_db_fatal(exc) else str(exc.mode)
        detail = str(exc)[:500]
        if is_log_db_fatal(exc):
            return EXIT_LOG_DB_FATAL
        raise
    except Exception as exc:
        reason, detail = type(exc).__name__, str(exc)[:500]
        raise
    finally:
        _report_to_controller(ns.run_id, ns.step, status, reason, detail)


def _report_to_controller(run_id, step, status: str, reason: str, detail: str) -> None:
    """State this work item's outcome where the Controller reads it.

    Never raises. The Worker is already on its way out; an exception here
    would replace the real reason with this function's own failure.
    """
    try:
        wi = get_frontend_mongo()[PIPELINE_ADMIN_DB]["pipeline.work_items"]
        wi.update_one(
            {"run_id": run_id, "step": step or os.environ.get("PIPELINE_STEP")},
            {"$set": {"status": status, "reason": reason, "detail": detail}},
        )
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
