# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Trigger and monitor the SnapshotCollection orchestration on Azure dev.

Server-side snapshot of one collection to another via Atlas-native
$out / $merge (never copies through the local cursor).

usage:
  python pipeline/run_snapshot_collection.py --source dev_PublicHealthData.providers --target dev_PublicHealthData.providers_snapshot_<date>
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
    parser = argparse.ArgumentParser(description="Run SnapshotCollection on Azure dev.")
    parser.add_argument("--source", required=True, help="db.collection to snapshot from.")
    parser.add_argument("--target", required=True, help="db.collection to snapshot to.")
    parser.add_argument(
        "--cluster", default="ChatHealthyDataPipelines",
        help="Atlas cluster name to reserve.",
    )
    parser.add_argument(
        "--duration-minutes", type=int, default=30,
        help="Reservation budget.",
    )
    args = parser.parse_args(argv)

    payload = {
        "pipeline_cluster": args.cluster,
        "expected_duration_minutes": args.duration_minutes,
        "env_prefix": "dev",
        "source_collection": args.source,
        "target_collection": args.target,
    }
    return run_pipeline("SnapshotCollection", payload)


if __name__ == "__main__":
    raise SystemExit(main())
