# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Trigger and monitor the ProviderPipeline orchestration on Azure dev.

Loads NPPES dissemination ZIP for the requested state(s), enriches with
county + urban + ICD-10 + proprietary flags + embeddings.

usage:
  python pipeline/run_provider_pipeline.py --states NE
  python pipeline/run_provider_pipeline.py --states NE --duration-minutes 90
  python pipeline/run_provider_pipeline.py --states NE MS DE

Default payload mirrors the canonical successful run recorded on the
durable-task instance history (`53c754417f26` 2026-05-22). The
orchestrator demands every one of these keys via `config["key"]` (no
`config.get` fallback in `provider_load_manager.py`), so all 21 must
travel together. Override any with the corresponding CLI flag below.
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
        help="Two-letter state codes (e.g., NE MS DE).",
    )
    parser.add_argument(
        "--start-step",
        default="Step 1: Reserving cluster",
        help="Canonical step label to start from.",
    )
    parser.add_argument(
        "--cluster", default="ChatHealthyDataPipelines",
        help="Atlas cluster name to reserve.",
    )
    parser.add_argument(
        "--duration-minutes", type=int, default=180,
        help="Reservation budget (NPPES ingest takes long for big states).",
    )

    # Collection FQNs (db.collection) — env-prefixed per the orchestrator
    # convention. Operator overrides per-env if needed.
    parser.add_argument(
        "--provider-collection",
        default="dev_PublicHealthData.pipeline_providers",
    )
    parser.add_argument(
        "--metadata-collection",
        default="admin.DataLoadMetadata",
    )
    parser.add_argument(
        "--pl-lookup-collection",
        default="dev_PublicHealthData.pl_pfile_lookup",
    )

    parser.add_argument("--blob-container", default="provider-data")
    parser.add_argument("--num-workers", type=int, default=100,
                        help="Parallel load workers (Step 3).")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="MongoDB bulk_write batch size per load worker.")
    parser.add_argument("--bulk-batch-size", type=int, default=1000)
    parser.add_argument("--incremental", action="store_true",
                        help="Incremental load mode.")
    parser.add_argument("--enrich-workers", type=int, default=4,
                        help="Parallel county-enrichment workers (Steps 5-9).")
    parser.add_argument("--addr-batch-size", type=int, default=200,
                        help="Providers per Census Geocoder batch (Passes 2-3, max 500).")
    parser.add_argument("--nppes-batch-size", type=int, default=100,
                        help="Providers per NPPES batch (Pass 6).")
    parser.add_argument("--reset-failed", action="store_true",
                        help="Reset geocoder_failed providers before Pass 2.")
    parser.add_argument("--google-maps", action="store_true",
                        help="Enable Pass 4 Google Maps geocoding (paid).")
    parser.add_argument("--embeddings", action=argparse.BooleanOptionalAction,
                        default=False, required=True,
                        help="Enable Step 11 embedding pass. Pass --embeddings "
                             "or --no-embeddings explicitly (no default).")
    parser.add_argument("--embed-model", default="text-embedding-3-large")
    parser.add_argument("--embed-batch-size", type=int, default=100)
    parser.add_argument("--embed-initial-jitter", type=int, default=0)

    args = parser.parse_args(argv)

    payload = {
        "pipeline_cluster": args.cluster,
        "expected_duration_minutes": args.duration_minutes,
        "env_prefix": "dev",
        "states": [s.upper() for s in args.states],
        "start_step": args.start_step,
        "provider_collection": args.provider_collection,
        "metadata_collection": args.metadata_collection,
        "pl_lookup_collection": args.pl_lookup_collection,
        "blob_container": args.blob_container,
        "num_workers": args.num_workers,
        "batch_size": args.batch_size,
        "bulk_batch_size": args.bulk_batch_size,
        "incremental": args.incremental,
        "enrich_workers": args.enrich_workers,
        "addr_batch_size": args.addr_batch_size,
        "nppes_batch_size": args.nppes_batch_size,
        "reset_failed": args.reset_failed,
        "google_maps_enabled": args.google_maps,
        "embedding_enabled": args.embeddings,
        "embed_model": args.embed_model,
        "embed_batch_size": args.embed_batch_size,
        "embed_initial_jitter": args.embed_initial_jitter,
    }
    return run_pipeline("ProviderPipeline", payload)


if __name__ == "__main__":
    raise SystemExit(main())
