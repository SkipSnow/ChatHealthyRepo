# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Trigger and monitor the CountyEnrichment orchestration on Azure dev.

Stamps county + urban flag onto provider records for the requested state(s).

usage:
  python pipeline/run_county_enrichment.py --states NE
  python pipeline/run_county_enrichment.py --states NE MS DE
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
    parser = argparse.ArgumentParser(description="Run CountyEnrichment on Azure dev.")
    parser.add_argument(
        "--states", nargs="+", required=True,
        help="Two-letter state codes (e.g., NE MS DE).",
    )
    parser.add_argument(
        "--cluster", default="ChatHealthyDataPipelines",
        help="Atlas cluster name to reserve.",
    )
    parser.add_argument(
        "--duration-minutes", type=int, default=60,
        help="Reservation budget.",
    )
    args = parser.parse_args(argv)

    payload = {
        "pipeline_cluster": args.cluster,
        "expected_duration_minutes": args.duration_minutes,
        "env_prefix": "dev",
        "states": [s.upper() for s in args.states],
    }
    return run_pipeline("CountyEnrichment", payload)


if __name__ == "__main__":
    raise SystemExit(main())
