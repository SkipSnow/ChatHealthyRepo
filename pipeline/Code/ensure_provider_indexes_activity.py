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
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402


import os
import time

from pymongo import MongoClient


# The provider collection's index set is declared in the record
# (deployment_architecture.json MongoIndexCatalog, entry pipeline_provider_staging)
# and applied by the one shared library chathealthy_lib.mongo_indexes. It is no
# longer restated here: a hardcoded list in pipeline code was the anti-pattern
# that let an illegal parallel-array index ship and reach production. In the
# pipeline container the declarations arrive as the derived pipeline_indexes.json
# baked at image-build time (the container carries no manifest); in the repo they
# are read from deployment_architecture.json directly. Same source either way.
def _pipeline_index_package_config() -> dict:
    """The pipeline_indexes mongo_indexes package config -- the fully-qualified
    entry {cluster, identity, db, collection, indexes, version_source}. In the
    pipeline container this is the derived pipeline_indexes.json baked at
    image-build (the container has no manifest); in the repo it is read from the
    pipeline_indexes package on target_atlas_pipeline. deployment_architecture.json
    is the one source; the baked file is its derived copy for the container."""
    import json  # noqa: PLC0415
    import pathlib  # noqa: PLC0415
    root = (pathlib.Path(__file__).resolve().parents[2]
            / "brain" / "machine_artifacts" / "content")
    derived = root / "pipeline_indexes.json"
    if derived.is_file():
        return json.loads(derived.read_text(encoding="utf-8"))
    manifest = root / "deployment_architecture.json"
    if manifest.is_file():
        arch = json.loads(manifest.read_text(encoding="utf-8"))
        for rec in arch.get("DeploymentTargetRecord", []):
            if rec.get("target_id") != "target_atlas_pipeline":
                continue
            for e in rec.get("environments", []):
                for p in (e.get("packages") or []):
                    if p.get("package_id") == "pipeline_indexes":
                        return p.get("config") or {}
    raise ChatHealthyException(
        mode="file_missing",
        component="ensure_provider_indexes_activity",
        message="neither the baked pipeline_indexes.json nor the pipeline_indexes "
                "package on target_atlas_pipeline is present to read the index specs")


def _pipeline_provider_index_specs() -> list:
    """The provider staging collection's declared indexes, from the record."""
    specs = _pipeline_index_package_config().get("indexes")
    if not specs:
        raise ChatHealthyException(
            mode="config_error",
            component="ensure_provider_indexes_activity",
            message="the pipeline_indexes package config carries no indexes")
    return specs


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
                raise ChatHealthyException(
                    mode="timeout",
                    component="ensure_provider_indexes_activity",
                    message=f"cluster not ready after {timeout_minutes} min "
                    f"({attempts} attempts): {exc}",
            exception=exc)
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
    # The PIPELINE cluster. Provider data -- PublicStaging and
    # PipelinePublicHealthData -- lives there. Opening the front end built
    # every index on a phantom collection MongoDB created on reference, so the
    # step reported success while the real staging collection, nine million
    # rows of it, kept no index at all. The ping loop above was equally
    # useless: it waited on the always-on front end rather than on the cluster
    # that has to come out of pause.
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    client = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyDataPipelines")
    return client[db_name][coll_name], client


def ensure_provider_indexes_fn(config: dict) -> dict:
    """Wait out the Atlas wake, then apply the record-declared provider indexes
    via the shared library. The index specs are the record's, not this file's;
    the applier is the one shared with the front end."""
    from chathealthy_lib.mongo_indexes import apply_indexes  # noqa: PLC0415
    coll, client = _providers_collection_and_client(config.get("provider_collection"))
    cluster_wait_minutes = int(config.get("cluster_wait_minutes", 20))
    _wait_for_cluster_ready(client, cluster_wait_minutes)
    results = apply_indexes(coll, _pipeline_provider_index_specs())
    return {
        "collection": coll.full_name,
        "indexes": results,
    }
