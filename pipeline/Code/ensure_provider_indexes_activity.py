# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ensure_provider_indexes_activity - software-managed provider indexes.

Called once per streaming_pipeline_orchestrator run, after the cluster is
awake and before the load fan-out starts. Idempotently creates every
index the pipeline depends on:

  npi_1                       sole entry point for per-NPI lookups in the
                              recovery activity (fetch_many_by_npi). Without
                              it, recovery scans the collection per NPI.

  addresses.county.source_1   multi-key index used by
                              build_recovery_assignments_fn's aggregation
                              that finds providers carrying any address
                              currently tagged geocoder_failed. Without it
                              the aggregation does a COLLSCAN -> Atlas
                              cursor timeout at scale.

create_index is idempotent on PyMongo — if the index already exists with
the same spec, it returns the existing name and does nothing. We do not
issue dropIndex; if a prior index spec drifted, the operator handles it
out-of-band.

Per Skip 2026-05-28: indexes are built by the software, not the operator.
No external prerequisite. No manual side-action.
"""
from __future__ import annotations

import logging
import os
import time

from pymongo import MongoClient


_REQUIRED_INDEXES = [
    {
        "keys": [("npi", 1)],
        "name": "npi_1",
        "background": True,
        "unique": False,
    },
    {
        "keys": [("addresses.county.source", 1)],
        "name": "addresses.county.source_1",
        "background": True,
        "unique": False,
    },
]


def _providers_collection(provider_collection: str | None):
    fqn = provider_collection or (
        f"{os.environ.get('ENV_PREFIX', 'dev')}_PublicHealthData.providers"
    )
    db_name, coll_name = fqn.split(".", 1)
    client = MongoClient(
        os.environ["MONGO_connectionString"],
        serverSelectionTimeoutMS=120_000,
    )
    return client[db_name][coll_name]


def ensure_provider_indexes_fn(config: dict) -> dict:
    coll = _providers_collection(config.get("provider_collection"))

    existing_names = set()
    try:
        for spec in coll.list_indexes():
            existing_names.add(spec.get("name"))
    except Exception as exc:
        logging.warning("ensure_provider_indexes: list_indexes failed: %s", exc)

    results: list[dict] = []
    for idx in _REQUIRED_INDEXES:
        name = idx["name"]
        already = name in existing_names
        t0 = time.time()
        try:
            created_name = coll.create_index(
                idx["keys"],
                name=name,
                background=idx["background"],
                unique=idx["unique"],
            )
            results.append({
                "name": created_name,
                "already_existed": already,
                "duration_seconds": round(time.time() - t0, 2),
            })
            logging.info(
                "ensure_provider_indexes: %s (already=%s) in %.1fs",
                created_name, already, time.time() - t0,
            )
        except Exception as exc:
            logging.error("ensure_provider_indexes: %s failed: %s", name, exc)
            results.append({
                "name": name,
                "already_existed": already,
                "error": str(exc)[:300],
            })

    return {
        "collection": coll.full_name,
        "indexes": results,
    }
