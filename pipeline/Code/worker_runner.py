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

import argparse
import logging
import os
import sys

from blob_client import get_blob_service
from pipeline_db import get_mongo
from pipeline_env import load_pipeline_env
from step_context import PipelineArgs, RunManifest, StepContext
from steps import get_runner

_log = logging.getLogger("worker_runner")

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


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv if argv is not None else sys.argv[1:])

    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
    root.setLevel(getattr(logging, ns.log_level.upper(), logging.INFO))

    if not ns.step:
        raise ValueError("worker_runner: --step or PIPELINE_STEP env var required")

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
        mongo_client=get_mongo(),
        blob_client=get_blob_service(),
    )
    runner = get_runner(ns.step)
    _log.info("worker_runner: step=%s run=%s partition=%s",
              ns.step, args.run_id, partition)
    result = runner(ctx)
    _log.info("worker_runner: step=%s complete keys=%s",
              ns.step,
              list(result.keys()) if isinstance(result, dict) else type(result).__name__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
