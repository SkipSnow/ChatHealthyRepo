# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ensure_provider_indexes_activity - software-managed provider indexes.

Called once per provider_pipeline_orchestrator run, after the cluster is
awake and before the load fan-out starts. Idempotently creates every
index the pipeline depends on:

  npi_1                       sole entry point for per-NPI lookups in the
                              recovery activity (fetch_many_by_npi). Without
                              it, recovery scans the collection per NPI.

  practice_addresses.county   used by build_recovery_assignments_fn's
  .source_1                   aggregation that finds providers carrying an
                              address currently tagged with a per-pass
                              failure label. Practice only: county
                              enrichment admits practice address types and
                              nothing else, so a business-address county
                              index would index a field no pass populates.
                              Without this the aggregation does a COLLSCAN
                              -> Atlas cursor timeout at scale.

  business_state_taxonomy     compound {business_address.state,
                              taxonomies.code}. Its prefix answers the
                              state-scoped drain and every per-state fan-out;
                              both fields answer the F-105 catalog join in
                              §5.2.16. One index doing two jobs, where the
                              array-based pair could do neither well: with
                              addresses[] and taxonomies[] both arrays, no
                              compound index could span them, so the planner
                              picked one and post-filtered with the other --
                              measured examining 600,248 documents to write
                              263.

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


# The provider collection's index set, per LLD v47 §7.6. This list is the one
# statement of it: provider_normalize_engine applies these rather than
# restating them, because the two sites previously created the same key under
# two different names and nothing reconciled them.
#
# {business_address.state, taxonomies.code} is one index doing two jobs. A
# compound index serves queries on its prefix, so the per-state drain and
# fan-out are answered by its first field alone, and the F-105 catalog join by
# both. It replaces {addresses.address_type, addresses.state} and
# {taxonomies.code}: those named two array paths, so no compound could span
# them, and the planner had to pick one and filter the other -- measured
# 2026-08-16 examining 600,248 documents to write 263.
_REQUIRED_INDEXES = [
    {
        "keys": [("npi", 1)],
        "name": "npi_1",
        "background": True,
        "unique": True,
    },
    {
        "keys": [("business_address.state", 1), ("taxonomies.code", 1)],
        "name": "business_state_taxonomy",
        "background": True,
        "unique": False,
    },
    {
        "keys": [("practice_addresses.county.source", 1)],
        "name": "practice_addresses.county.source_1",
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
    client = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")
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
