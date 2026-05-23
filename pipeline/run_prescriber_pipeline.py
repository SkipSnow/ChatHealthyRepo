# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Trigger and monitor the PrescriberEvaluateCarePipeline orchestration.

Loads CMS Part D + OIG LEIE + SAM.gov, crosswalks molecule→indication→ICD-10,
emits specialty baselines, embeds prescriber drug/molecule rollups.

usage:
  python pipeline/run_prescriber_pipeline.py
  python pipeline/run_prescriber_pipeline.py --steps "Step 4: Load provider_quality" "Step 5: Crosswalk — molecule→indication→ICD-10"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _router_client import run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run PrescriberEvaluateCarePipeline on Azure dev.")
    parser.add_argument(
        "--steps", nargs="+", default=None,
        help="Optional canonical step labels (subset to run).",
    )
    parser.add_argument(
        "--cluster", default="ChatHealthyDataPipelines",
        help="Atlas cluster name to reserve.",
    )
    parser.add_argument(
        "--duration-minutes", type=int, default=240,
        help="Reservation budget (prescriber pipeline is long).",
    )
    args = parser.parse_args(argv)

    payload = {
        "pipeline_cluster": args.cluster,
        "expected_duration_minutes": args.duration_minutes,
        "env_prefix": "dev",
    }
    if args.steps:
        payload["steps"] = args.steps
    return run_pipeline("PrescriberEvaluateCarePipeline", payload)


if __name__ == "__main__":
    raise SystemExit(main())
