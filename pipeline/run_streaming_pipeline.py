# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Trigger the records-as-messages StreamingPipeline orchestration on dev.

Spike scope (2026-05-26): raw-parse → normalize → pass-1-ZIP → mongo-write
inline in one per-partition activity. No store-and-forward via Mongo between
stages. Target throughput: 15 records/sec/worker sustained, flat-line.

usage:
  python pipeline/run_streaming_pipeline.py --states MO \
      --nppes-refill-rate 5.0 --nppes-capacity 10 \
      --maps-refill-rate 10.0 --maps-capacity 50 \
      --openai-refill-rate 10.0 --openai-capacity 100
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
    parser = argparse.ArgumentParser(description="Run StreamingPipeline on Azure dev.")
    parser.add_argument(
        "--states", nargs="+", required=True,
        help="Two-letter state codes (MO for the spike).",
    )
    parser.add_argument(
        "--cluster", default="ChatHealthyDataPipelines",
        help="Atlas cluster name to reserve.",
    )
    parser.add_argument(
        "--duration-minutes", type=int, default=60,
        help="Reservation budget. MO is small (~22K records); 60 min is conservative.",
    )
    parser.add_argument(
        "--provider-collection",
        default="dev_PublicHealthData.pipeline_providers",
    )
    parser.add_argument(
        "--metadata-collection",
        default="admin.DataLoadMetadata",
    )
    parser.add_argument("--blob-container", default="provider-data")
    parser.add_argument("--num-workers", type=int, default=100,
                        help="Parallel partition activities.")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="MongoDB bulk_write batch size per partition activity.")

    # Throttle params — required to mirror the legacy run script's contract
    # even though the spike doesn't yet call acquire() from the partition
    # activity. Entities are configured-and-quiescent.
    parser.add_argument("--nppes-refill-rate", type=float, required=True)
    parser.add_argument("--nppes-capacity", type=int, required=True)
    parser.add_argument("--maps-refill-rate", type=float, required=True)
    parser.add_argument("--maps-capacity", type=int, required=True)
    parser.add_argument("--openai-refill-rate", type=float, required=True)
    parser.add_argument("--openai-capacity", type=int, required=True)

    args = parser.parse_args(argv)

    payload = {
        "pipeline_cluster":          args.cluster,
        "expected_duration_minutes": args.duration_minutes,
        "env_prefix":                "dev",
        "states":                    [s.upper() for s in args.states],
        "provider_collection":       args.provider_collection,
        "metadata_collection":       args.metadata_collection,
        "blob_container":            args.blob_container,
        "num_workers":               args.num_workers,
        "batch_size":                args.batch_size,
        "throttle": {
            "nppes":      {"refill_rate": args.nppes_refill_rate,  "capacity": args.nppes_capacity},
            "google_maps":{"refill_rate": args.maps_refill_rate,   "capacity": args.maps_capacity},
            "openai":     {"refill_rate": args.openai_refill_rate, "capacity": args.openai_capacity},
        },
    }
    return run_pipeline("StreamingPipeline", payload)


if __name__ == "__main__":
    raise SystemExit(main())
