# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Universal load-state tracking for pipeline PipelinePublicHealthData collections.

Operator rules 2026-08-02:
  * Loaded = migrated from staging to PipelinePublicHealthData on the pipeline
    cluster. Staging collections live in ChatHealthyDataPipelines.
    PublicStaging; loaded collections live in ChatHealthyDataPipelines.
    PipelinePublicHealthData.
  * All collections versioned with the right integer version number.
  * Versioning is part of the key: metadata _id = versioned collection
    name (e.g. "SpecialtyMetaData_v_3"). One metadata record per version.
  * Load-state metadata lives on the FRONT-END cluster
    (pipelineAdmin.pipeline.loaded_metadata on the front-end cluster). Rationale: frontend
    cluster is always awake even when the pipeline cluster is paused
    between fires, so freshness lookups + skip decisions can happen
    before waking Atlas. Same placement pattern as every other
    pipeline-orchestration collection (pipeline.runs, pipeline.config,
    pipeline.discrepancies, pipeline.work_items).
  * A step SKIPS the load iff ALL of these are true:
      - source hash matches metadata.source_hash
      - metadata.operationally_fit is True
      - the loaded PipelinePublicHealthData collection actually exists on the
        pipeline cluster (parity check against physical presence)
      - metadata.row_count matches actual row count on the collection
  * Non-fatal errors -> mark loaded (operationally_fit=True), don't
    reload on next fire.
  * Fatal errors -> don't write metadata -> absence signals the next
    fire to reload.

Every pipeline step that produces a PipelinePublicHealthData collection uses
this module: `should_skip()` at entry, `mark_loaded()` at exit.
Callers pass BOTH mongo clients: `frontend` for the metadata coll
(pipelineAdmin), `pipeline` for the physical PipelinePublicHealthData
collection presence + row-count checks.
"""

from __future__ import annotations

from datetime import datetime, timezone


_METADATA_DB = "pipelineAdmin"  # metadata home, front-end cluster
_METADATA_COLL = "pipeline.loaded_metadata"


def read_metadata(frontend, publichealthdata_collection_name: str) -> dict | None:
    """Return the load-metadata doc for the versioned PipelinePublicHealthData
    collection (e.g. 'SpecialtyMetaData_v_3'), or None if no prior
    successful load exists. Reads from the FRONTEND cluster."""
    return frontend[_METADATA_DB][_METADATA_COLL].find_one(
        {"_id": publichealthdata_collection_name}
    )


def publichealthdata_collection_exists(
    pipeline_mongo,
    registry,
    source_name: str,
    collection_name: str,
) -> bool:
    # public_data_db comes from the registry entry, not a module constant --
    # each dataset_versions[] source owns its own destination DB.
    entry = registry.by_source_name(source_name)
    return collection_name in pipeline_mongo[entry.public_data_db].list_collection_names()


def should_skip(
    *,
    pipeline_mongo,
    frontend_mongo,
    registry,
    source_name: str,
    publichealthdata_collection_name: str,
    current_source_hash: str,
) -> tuple[bool, str]:
    """Return (skip: bool, reason: str). Skip iff ALL four:
      1. current_source_hash non-empty AND matches metadata.source_hash
         (metadata on frontend cluster)
      2. metadata.operationally_fit is True
      3. the versioned PipelinePublicHealthData collection exists on the
         pipeline cluster (physical check, not metadata) -- the DB name
         comes from registry.by_source_name(source_name).public_data_db
      4. actual row count on that collection == metadata.row_count
         (parity check catches out-of-band drops / truncates / drift)

    Any failure means the step must run and reload.
    """
    if not current_source_hash:
        return False, "no current source hash available"
    meta = read_metadata(frontend_mongo, publichealthdata_collection_name)
    if not meta:
        return False, f"no prior load metadata for {publichealthdata_collection_name!r}"
    if meta.get("source_hash") != current_source_hash:
        return False, (
            f"source hash mismatch (loaded={meta.get('source_hash')!r} "
            f"current={current_source_hash!r})"
        )
    if not meta.get("operationally_fit"):
        return False, "prior load not marked operationally_fit"
    if not publichealthdata_collection_exists(
        pipeline_mongo, registry, source_name, publichealthdata_collection_name,
    ):
        return False, (
            f"PipelinePublicHealthData collection {publichealthdata_collection_name!r} "
            f"does not exist on pipeline cluster"
        )
    entry = registry.by_source_name(source_name)
    recorded_rows = meta.get("row_count")
    actual_rows = pipeline_mongo[entry.public_data_db][
        publichealthdata_collection_name
    ].count_documents({})
    if recorded_rows != actual_rows:
        return False, (
            f"row-count parity mismatch (metadata.row_count={recorded_rows} "
            f"actual={actual_rows}) -- collection touched out-of-band since load"
        )
    return True, (
        f"hash matches ({current_source_hash!r}), collection "
        f"{publichealthdata_collection_name!r} exists with {actual_rows} rows "
        f"matching metadata, operationally_fit=True"
    )


def mark_loaded(
    *,
    frontend_mongo,
    publichealthdata_collection_name: str,
    staging_collection_name: str,
    source_hash: str,
    run_id: str,
    data_version: int,
    row_count: int,
    operationally_fit: bool = True,
    detail: dict | None = None,
) -> None:
    """Upsert load metadata for the versioned PipelinePublicHealthData collection.
    Writes to the FRONTEND cluster (pipelineAdmin.pipeline.
    loaded_metadata). Called at the end of a step that produced the
    collection without a fatal error.

    Both source staging collection and target loaded collection names
    are recorded so an operator can read the metadata doc alone and
    know the full migration lineage.

    The `run_id` field matches the `run_id` field stamped on each
    individual data record in the loaded collection -- operators can
    trace any row back to its metadata doc and vice versa.

    A step MUST NOT call this on a fatal-error path -- absence of the
    doc is the signal that the next fire should reload.
    """
    frontend_mongo[_METADATA_DB][_METADATA_COLL].update_one(
        {"_id": publichealthdata_collection_name},
        {
            "$set": {
                "publichealthdata_collection_name": publichealthdata_collection_name,
                "staging_collection_name": staging_collection_name,
                "source_hash": source_hash,
                "loaded_at": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id,
                "data_version": data_version,
                "row_count": row_count,
                "operationally_fit": operationally_fit,
                "detail": detail or {},
            }
        },
        upsert=True,
    )
