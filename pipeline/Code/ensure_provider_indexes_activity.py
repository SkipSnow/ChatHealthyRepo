# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ensure_provider_indexes_activity - software-managed provider indexes.

Called once per provider_pipeline_orchestrator run, after the cluster is
awake and before the load fan-out starts. Idempotently creates every
index the pipeline depends on:

  npi_1                       sole entry point for per-NPI lookups in the
                              recovery activity (fetch_many_by_npi). Without
                              it, recovery scans the collection per NPI.

  addresses.county.source_1   multi-key index used by
                              build_recovery_assignments_fn's aggregation
                              that finds providers carrying any address
                              currently tagged with a per-pass failure
                              label. Without it the aggregation does a
                              COLLSCAN -> Atlas cursor timeout at scale.

  addresses.address_type_1_   compound multi-key index supporting drain's
  addresses.state_1           $elemMatch on (address_type='business',
                              state in [...]) — the canonical residency
                              signal for state-scoped drains. Without it
                              drain COLLSCAN'd all 1.5M docs to find the
                              handful of CA business-address matches.

create_index is idempotent on PyMongo — if the index already exists with
the same spec, it returns the existing name and does nothing. We do not
issue dropIndex; if a prior index spec drifted, the operator handles it
out-of-band.

The activity rides out Atlas wake/REPAIRING windows by polling the
cluster every 5 seconds via _wait_for_cluster_ready until a configurable
timeout (default 20 minutes) elapses. On timeout the activity raises;
the orchestrator's try/finally then releases the reservation cleanly.

Per Skip 2026-05-28: indexes are built by the software, not the operator.
No external prerequisite. No manual side-action.
"""
from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService


import os
import time

from pymongo import MongoClient


_REQUIRED_INDEXES = [
    {
        "keys": [("npi", 1)],
        "name": "npi_1",
        "background": True,
        "unique": True,
    },
    {
        "keys": [("addresses.county.source", 1)],
        "name": "addresses.county.source_1",
        "background": True,
        "unique": False,
    },
    {
        "keys": [("addresses.address_type", 1), ("addresses.state", 1)],
        "name": "addresses.address_type_1_addresses.state_1",
        "background": True,
        "unique": False,
    },
]


def _wait_for_cluster_ready(
    client: MongoClient,
    timeout_minutes: int,
    poll_seconds: int = 5,
) -> None:
    """Poll the cluster every poll_seconds with admin.ping until it
    answers cleanly, or raise TimeoutError after timeout_minutes.

    Sized to ride out Atlas paused->wake transitions (cluster goes
    through REPAIRING and replicas come online one at a time). The
    function is a deterministic timer, so it needs no retry machinery
    of its own.
    """
    deadline = time.time() + timeout_minutes * 60
    attempts = 0
    while True:
        attempts += 1
        try:
            client.admin.command("ping")
            ChatHealthyLoggingService().info(
                "cluster ready after %d attempt(s) (~%.0fs)",
                attempts, attempts * poll_seconds,
            )
            return
        except Exception as exc:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"cluster not ready after {timeout_minutes} min "
                    f"({attempts} attempts): {exc}"
                )
            ChatHealthyLoggingService().info(
                "cluster not ready (attempt %d, %.0fs remaining): %s",
                attempts, remaining, exc,
            )
            time.sleep(poll_seconds)


def _providers_collection_and_client(provider_collection: str | None) -> tuple:
    fqn = provider_collection or "PipelinePublicHealthData.providers"
    db_name, coll_name = fqn.split(".", 1)
    # serverSelectionTimeoutMS is short so each ping fails fast and the
    # _wait_for_cluster_ready poll loop drives the cadence.
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    client = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "frontEnd")
    return client[db_name][coll_name], client


def ensure_provider_indexes_fn(config: dict) -> dict:
    coll, client = _providers_collection_and_client(config.get("provider_collection"))
    cluster_wait_minutes = int(config.get("cluster_wait_minutes", 20))
    _wait_for_cluster_ready(client, cluster_wait_minutes)

    existing_names = set()
    try:
        for spec in coll.list_indexes():
            existing_names.add(spec.get("name"))
    except Exception as exc:
        ChatHealthyLoggingService().warning("ensure_provider_indexes: list_indexes failed: %s", exc)

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
            ChatHealthyLoggingService().info(
                "ensure_provider_indexes: %s (already=%s) in %.1fs",
                created_name, already, time.time() - t0,
            )
        except Exception as exc:
            ChatHealthyLoggingService().error("ensure_provider_indexes: %s failed: %s", name, exc)
            results.append({
                "name": name,
                "already_existed": already,
                "error": str(exc)[:300],
            })

    return {
        "collection": coll.full_name,
        "indexes": results,
    }
