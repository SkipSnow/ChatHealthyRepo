# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ACA Control tier entrypoint — LLD v22 prov-control job."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger("control_runner")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pipeline_env import load_pipeline_env

load_pipeline_env()
os.environ.setdefault("PROVIDER_FLAGS_COLLECTION", "SpecialtyAndProviderFlags_draft")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provider pipeline LLD v22 control runner")
    parser.add_argument("--states", nargs="+", required=True)
    parser.add_argument("--env-prefix", default=os.environ.get("ENV_PREFIX", "dev"))
    parser.add_argument("--duration-minutes", type=int, default=120)
    parser.add_argument("--provider-collection", default=None)
    parser.add_argument("--resume-from-step", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--google-maps-enabled", action="store_true")
    parser.add_argument("--pool-size", type=int, default=51)
    parser.add_argument("--incremental", action="store_true")
    args = parser.parse_args(argv)

    from pipeline_db import get_mongo
    from provider_pipeline_orchestrator import ProviderPipelineOrchestrator
    from step_context import PipelineArgs

    pipeline_args = PipelineArgs(
        states=[s.upper() for s in args.states],
        env_prefix=args.env_prefix,
        expected_duration_minutes=args.duration_minutes,
        provider_collection=args.provider_collection,
        google_maps_enabled=args.google_maps_enabled,
        pool_size=args.pool_size,
        resume_from_step=args.resume_from_step,
        run_id=args.run_id,
        incremental=args.incremental,
    )

    orch = ProviderPipelineOrchestrator(
        env=args.env_prefix,
        config={},
        mongo_client=get_mongo(),
        blob_client=None,
    )
    manifest = orch.run(pipeline_args)
    manifest.metrics["states"] = pipeline_args.resolved_states()
    print(json.dumps({
        "run_id": manifest.run_id,
        "status": manifest.status,
        "completed_steps": sorted(manifest.completed_steps),
        "metrics": manifest.metrics,
    }, indent=2))
    return 0 if manifest.status in ("completed", "quiesced") else 1


if __name__ == "__main__":
    raise SystemExit(main())
