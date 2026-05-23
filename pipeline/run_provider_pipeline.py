# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Trigger and monitor the ProviderPipeline orchestration on Azure dev.

Loads NPPES dissemination ZIP for the requested state(s), enriches with
county + urban + ICD-10 + proprietary flags.

usage:
  python pipeline/run_provider_pipeline.py --states NE
  python pipeline/run_provider_pipeline.py --states NE MS DE
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
    parser = argparse.ArgumentParser(description="Run ProviderPipeline on Azure dev.")
    parser.add_argument(
        "--states", nargs="+", required=True,
        help="One or more two-letter state codes (e.g., NE MS DE).",
    )
    parser.add_argument(
        "--cluster", default="ChatHealthyDataPipelines",
        help="Atlas cluster name to reserve.",
    )
    parser.add_argument(
        "--duration-minutes", type=int, default=180,
        help="Reservation budget (NPPES ingest takes long for big states).",
    )
    parser.add_argument(
        "--num-workers", type=int, default=100,
        help="Loader worker count (operator baseline = 100).",
    )
    parser.add_argument(
        "--start-step", default=None,
        help="Optional canonical step label to start from.",
    )
    args = parser.parse_args(argv)

    payload = {
        "pipeline_cluster": args.cluster,
        "expected_duration_minutes": args.duration_minutes,
        "env_prefix": "dev",
        "states": [s.upper() for s in args.states],
        "num_workers": args.num_workers,
    }
    if args.start_step:
        payload["start_step"] = args.start_step
    return run_pipeline("ProviderPipeline", payload)


if __name__ == "__main__":
    raise SystemExit(main())
