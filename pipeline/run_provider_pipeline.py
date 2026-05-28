# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Trigger the ProviderPipeline orchestration.

The ProviderPipeline task is the one and only code path that produces
the front-end providers collection ({env}_PublicHealthData.providers).
It runs end-to-end (load + recovery + embeddings + reservation
lifecycle) inside a single orchestrator hierarchy with no external
sub-orchestration calls.
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _router_client import run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run ProviderPipeline on Azure dev.")
    parser.add_argument("--states", nargs="+", required=True)
    parser.add_argument("--cluster", default="ChatHealthyDataPipelines")
    parser.add_argument("--duration-minutes", type=int, default=120)
    parser.add_argument(
        "--provider-collection",
        default="dev_PublicHealthData.providers",
    )
    parser.add_argument("--metadata-collection", default="admin.DataLoadMetadata")
    parser.add_argument("--blob-container", default="provider-data")
    parser.add_argument("--pool-size", type=int, default=None,
                        help="Number of record-processing worker sub-orchestrations.")
    parser.add_argument("--num-workers", type=int, default=None,
                        help="DEPRECATED alias for --pool-size; will be removed.")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Mongo bulk_write batch size per assignment.")
    parser.add_argument("--discrepancy-abort-threshold", type=int, default=1000,
                        help="Pipeline aborts when discrepancy count exceeds this.")
    parser.add_argument("--chunk-size-bytes", type=int, default=2_500_000)

    parser.add_argument("--census-refill-rate", type=float, default=10.0)
    parser.add_argument("--census-capacity", type=int, default=20)
    parser.add_argument("--nppes-refill-rate", type=float, required=True)
    parser.add_argument("--nppes-capacity", type=int, required=True)
    parser.add_argument("--maps-refill-rate", type=float, required=True)
    parser.add_argument("--maps-capacity", type=int, required=True)
    parser.add_argument("--openai-refill-rate", type=float, required=True)
    parser.add_argument("--openai-capacity", type=int, required=True)

    args = parser.parse_args(argv)

    if args.pool_size is None and args.num_workers is None:
        pool_size = 100
    elif args.pool_size is not None:
        pool_size = args.pool_size
        if args.num_workers is not None:
            warnings.warn("--num-workers is deprecated; use --pool-size only", DeprecationWarning)
    else:
        warnings.warn("--num-workers is deprecated; use --pool-size", DeprecationWarning)
        pool_size = args.num_workers

    payload = {
        "pipeline_cluster": args.cluster,
        "expected_duration_minutes": args.duration_minutes,
        "env_prefix": "dev",
        "states": [s.upper() for s in args.states],
        "provider_collection": args.provider_collection,
        "metadata_collection": args.metadata_collection,
        "blob_container": args.blob_container,
        "pool_size": pool_size,
        "num_workers": pool_size,
        "batch_size": args.batch_size,
        "discrepancy_threshold": args.discrepancy_abort_threshold,
        "chunk_size_bytes": args.chunk_size_bytes,
        "throttle": {
            "census": {"refill_rate": args.census_refill_rate, "capacity": args.census_capacity},
            "nppes": {"refill_rate": args.nppes_refill_rate, "capacity": args.nppes_capacity},
            "google_maps": {"refill_rate": args.maps_refill_rate, "capacity": args.maps_capacity},
            "openai": {"refill_rate": args.openai_refill_rate, "capacity": args.openai_capacity},
            "pool_size": {"refill_rate": float(pool_size), "capacity": pool_size},
            "source_gather": {"refill_rate": 2.0, "capacity": 6},
        },
    }
    return run_pipeline("ProviderPipeline", payload)


if __name__ == "__main__":
    raise SystemExit(main())
