# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Universal load-state tracking for pipeline PublicHealthData collections.

Operator rules 2026-08-02:
  * Loaded = migrated from staging to PublicHealthData on the pipeline
    cluster.
  * All files versioned with the right version number.
  * Versioning is part of the key: metadata _id = versioned collection
    name (e.g. "SpecialtyMetaData_v_3"). One metadata record per version.
  * A step SKIPS the load if ALL of these are true:
      - source hash matches metadata.source_hash
      - collection actually exists on PublicHealthData
      - metadata.row_count matches the actual row count on the collection
      - metadata.operationally_fit is True
  * Non-fatal errors -> mark loaded (operationally_fit=True), don't
    reload on next fire.
  * Fatal errors -> don't write metadata -> absence signals the next
    fire to reload.

Every pipeline step that produces a PublicHealthData collection uses
this module: `should_skip()` at entry, `mark_loaded()` at exit.
"""

from __future__ import annotations

from datetime import datetime, timezone


_PUBLIC_DB = "PublicHealthData"
_METADATA_COLL = "_loaded_metadata"


def read_metadata(mongo, publichealthdata_collection_name: str) -> dict | None:
    """Return the load-metadata doc for the versioned PublicHealthData
    collection (e.g. 'SpecialtyMetaData_v_3'), or None if no prior
    successful load exists.
    """
    return mongo[_PUBLIC_DB][_METADATA_COLL].find_one(
        {"_id": publichealthdata_collection_name}
    )


def publichealthdata_collection_exists(mongo, collection_name: str) -> bool:
    return collection_name in mongo[_PUBLIC_DB].list_collection_names()


def should_skip(
    mongo,
    *,
    publichealthdata_collection_name: str,
    current_source_hash: str,
) -> tuple[bool, str]:
    """Return (skip: bool, reason: str). Skip iff ALL four:
      1. current_source_hash is non-empty AND matches metadata.source_hash
      2. metadata.operationally_fit is True
      3. the versioned PublicHealthData collection exists on the
         pipeline cluster
      4. actual row count on that collection == metadata.row_count
         (parity check catches out-of-band drops / truncates / drift)

    Any failure means the step must run and reload.
    """
    if not current_source_hash:
        return False, "no current source hash available"
    meta = read_metadata(mongo, publichealthdata_collection_name)
    if not meta:
        return False, f"no prior load metadata for {publichealthdata_collection_name!r}"
    if meta.get("source_hash") != current_source_hash:
        return False, (
            f"source hash mismatch (loaded={meta.get('source_hash')!r} "
            f"current={current_source_hash!r})"
        )
    if not meta.get("operationally_fit"):
        return False, "prior load not marked operationally_fit"
    if not publichealthdata_collection_exists(mongo, publichealthdata_collection_name):
        return False, (
            f"PublicHealthData collection {publichealthdata_collection_name!r} "
            f"does not exist"
        )
    recorded_rows = meta.get("row_count")
    actual_rows = mongo[_PUBLIC_DB][publichealthdata_collection_name].count_documents({})
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
    mongo,
    *,
    publichealthdata_collection_name: str,
    staging_collection_name: str,
    source_hash: str,
    run_id: str,
    data_version: int,
    row_count: int,
    operationally_fit: bool = True,
    detail: dict | None = None,
) -> None:
    """Upsert load metadata for the versioned PublicHealthData collection.
    Called at the end of a step that produced the collection without a
    fatal error. operationally_fit=True per operator rule (non-fatal
    errors still mark the collection loaded and usable).

    Both the source staging collection and the target PublicHealthData
    collection are recorded so an operator can read the metadata doc
    alone and know the full migration lineage.

    The `run_id` field matches the `run_id` field stamped on each
    individual data record in the loaded collection -- operators can
    trace any row back to its metadata doc and vice versa.

    A step MUST NOT call this on a fatal-error path -- absence of the
    doc is the signal that the next fire should reload.
    """
    mongo[_PUBLIC_DB][_METADATA_COLL].update_one(
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
