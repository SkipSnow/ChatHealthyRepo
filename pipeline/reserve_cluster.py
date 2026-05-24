# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Make a manual cluster reservation against ChatHealthyDataPipelines.

By default registers a 'human' class reservation so the 5-minute auto-
reaper leaves it alone. Use this to hold the cluster for manual data
work or migrations — pipelines won't be able to start (their own
reservation gate blocks until the cluster is IDLE again).

usage:
  python pipeline/reserve_cluster.py                     # 3-hour human reservation
  python pipeline/reserve_cluster.py --hours 6
  python pipeline/reserve_cluster.py --requester skip --hours 1
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CODE_DIR = REPO_ROOT / "pipeline" / "Code"
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

# Force UTF-8 stdout for Windows consoles.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Load Code/.env into os.environ so MONGO_connectionString + ATLAS_* creds
# are visible to cluster_lifecycle_manager + pipeline_db.
ENV_FILE = REPO_ROOT / "Code" / ".env"
if ENV_FILE.is_file():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from cluster_lifecycle_manager import ClusterLifecycleManager  # noqa: E402
from pipeline_db import get_frontend_mongo  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Reserve the pipeline cluster manually.")
    ap.add_argument("--hours", type=float, default=3.0,
                    help="Reservation duration in hours (default 3).")
    ap.add_argument("--cluster", default="ChatHealthyDataPipelines",
                    help="Atlas cluster name.")
    ap.add_argument("--requester", default="operator",
                    help="Free-text label stamped on the reservation.")
    ap.add_argument("--env-prefix", default="dev",
                    help="Env prefix selects the System db: <env>_System.cluster_lifecycle.")
    ap.add_argument(
        "--class", dest="cls", default="human",
        choices=["human", "automated"],
        help="'human' reservations are never auto-reaped by the timer; "
             "'automated' reservations are reaped on expected_end_time pass.",
    )
    args = ap.parse_args(argv)

    minutes = max(1, int(round(args.hours * 60)))
    job_id = f"{args.cls}-{uuid.uuid4().hex[:12]}"

    print(f"[reserve_cluster] cluster={args.cluster} duration={minutes}min "
          f"class={args.cls} requester={args.requester} job_id={job_id}",
          flush=True)

    mgr = ClusterLifecycleManager(get_db_fn=get_frontend_mongo)
    result = mgr.reserve(
        cluster_name=args.cluster,
        job_id=job_id,
        requester=args.requester,
        expected_duration_minutes=minutes,
        reservation_class=args.cls,
    )
    print(f"[reserve_cluster] result: {result}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
