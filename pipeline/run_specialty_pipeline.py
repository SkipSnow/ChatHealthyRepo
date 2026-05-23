# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Trigger and monitor the SpecialtyPipeline orchestration on Azure dev.

Loads the NUCC provider taxonomy CSV into PublicHealthData.SpecialtyMetaData,
embeds each record (text-embedding-3-large), then enriches with
can_prescribe + is_homeopathic flags from the classification catalog.

usage:
  python pipeline/run_specialty_pipeline.py
  python pipeline/run_specialty_pipeline.py --no-embeddings
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
    parser = argparse.ArgumentParser(description="Run SpecialtyPipeline on Azure dev.")
    parser.add_argument(
        "--no-embeddings", action="store_true",
        help="Skip the OpenAI embedding pass (cheap-test runs).",
    )
    parser.add_argument(
        "--cluster", default="ChatHealthyDataPipelines",
        help="Atlas cluster name to reserve.",
    )
    parser.add_argument(
        "--duration-minutes", type=int, default=15,
        help="Reservation budget (orchestrator releases automatically).",
    )
    args = parser.parse_args(argv)

    payload = {
        "pipeline_cluster": args.cluster,
        "expected_duration_minutes": args.duration_minutes,
        "env_prefix": "dev",
        "embedding_enabled": not args.no_embeddings,
    }
    return run_pipeline("SpecialtyPipeline", payload)


if __name__ == "__main__":
    raise SystemExit(main())
