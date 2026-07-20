# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Provider Pipeline LLD v23 §2.2 — ACA Control container entry point.

Invoked by bootstrap.py inside the prov-control ACA Job as
    python control_runner.py --run-id ... --env-prefix ... [--resume-from-step ...]
The Runbook (§2.1) has already written the run manifest to
chathealthyfrontend.pipeline.runs; this process picks it up, instantiates
ProviderPipelineOrchestrator, and drives every stage.

Local runs (developer workstation) invoke this same entry point; the
aca_job_manager helper detects PIPELINE_LOCAL_MODE and no-ops the ARM
calls.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from blob_client import get_blob_service
from pipeline_db import get_mongo
from pipeline_env import load_pipeline_env
from provider_pipeline_orchestrator import ProviderPipelineOrchestrator
from step_context import PipelineArgs

_log = logging.getLogger("control_runner")


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
    return parser.parse_args(argv)


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

    load_pipeline_env()

    states_list = _states_list(ns.states)
    incremental = (ns.load_mode == "incremental")
    args = PipelineArgs(
        states=states_list,
        env_prefix=ns.env_prefix,
        expected_duration_minutes=ns.expected_duration_minutes,
        resume_from_step=ns.resume_from_step,
        run_id=ns.run_id,
        incremental=incremental,
    )

    orchestrator = ProviderPipelineOrchestrator(
        env=ns.env_prefix,
        config={},
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
    manifest = orchestrator.run(args)
    if manifest.run_id:
        os.environ["RUN_ID"] = manifest.run_id
    _log.info("control_runner: run %s finished status=%s",
              manifest.run_id, manifest.status)
    return 0 if manifest.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
