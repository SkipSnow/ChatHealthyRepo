# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Publish StagingNucc -> PipelinePublicHealthData.SpecialtyMetaData_v_N + embeddings.

Realizes Provider Pipeline LLD v42 §5.2.8a. Runs after normalize_nucc.

**Pipeline-cluster-only.** Pipelines never migrate data from the pipeline
back-end cluster to the front-end cluster (operator directive 2026-08-02).
Every write in this step targets ChatHealthyDataPipelines. A separate
migrator job (out of scope for this step) is the sole legal path from
pipeline -> frontend.

Sequence inside execute():

  1. Copy every row from PublicStaging.StagingNucc_v_{data_version} into
     PipelinePublicHealthData.SpecialtyMetaData_staging_v_{data_version} on the
     same (pipeline) cluster. Strips pipeline-only fields (_id, run_id,
     _source_row_index, raw).
  2. Generate a text-embedding-3-large embedding for every staged row
     (embedding_engine.generate_specialty_embeddings). Stamps embedding,
     embedding_model, embedding_generated_at on each doc.
  3. Atomic swap:
        PipelinePublicHealthData.SpecialtyMetaData_staging_v_{n}
         --> PipelinePublicHealthData.SpecialtyMetaData_v_{n}
     via Collection.rename(new_name, dropTarget=True). Same-DB (renameCollection
     requires it). This IS the "loaded" transition per operator rule
     2026-08-02: "loaded means migrated from staging to PipelinePublicHealthData
     in the pipeline cluster." Non-fatal errors (like embed 429s) still
     mark the collection loaded; fatal errors block the rename so the
     next fire reloads.

Post-swap: PipelinePublicHealthData.SpecialtyMetaData_v_{n} holds 884 rows (883
NUCC + F-105 supplements) each with an embedding vector (or discrepancy
records for any rows the embed API dropped). The migrator, running on
its own schedule, is responsible for shipping this to the front-end
cluster for user-facing $vectorSearch (EPIC-002-F-005-S-001-REQ-T-003).

All collection names are version-suffixed (_v_N) per operator rule
"all files must be versioned with the right version number."

No polling, no locks, no retry; each PipelinePublicHealthData collection has
exactly one producing pipeline and cross-run collision is prevented
upstream by the Atlas cluster reservation semaphore.
"""

from __future__ import annotations

import os

from chathealthy_lib.exceptions import ChatHealthyException

from embedding_engine import generate_specialty_embeddings
from pipeline_loaded_metadata import mark_loaded, should_skip
from pipeline_runtime import PipelineRuntime
from staging_loader import staging_collection_name, staging_db_name


def _current_nucc_source_hash(ctx) -> str:
    """Extract the NUCC source-version identifier from this run's metrics.

    Populated by source_freshness_gate + fetch_all_sources earlier in the
    pipeline. Shape: 'etag=...;lastmod=...;clen=...' -- a stable
    identifier across fires of the same underlying source. When empty,
    the hash gate treats it as 'unknown' and forces a load.
    """
    manifest = getattr(ctx, "manifest", None)
    metrics = (getattr(manifest, "metrics", None) or {}) if manifest else {}
    freshness = metrics.get("source_freshness") or {}
    nucc = freshness.get("nucc") or {}
    return (nucc.get("current_version") or "").strip()


def _bail_openai_key_missing() -> None:
    raise ChatHealthyException(
        mode="runtime_error",
        message=(
            "publish_smd_and_embed: OPENAI_API_KEY is not set in the "
            "pipeline runtime environment. The embedding step cannot run "
            "without it and the SMD publish would ship rows without the "
            "embedding vector FindCare's $vectorSearch requires."
        ),
    )


def execute(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    dv = rt.data_version
    smd_entry = rt.registry.by_source_name("smd")
    staging_db = smd_entry.staging_db
    loaded_db = smd_entry.public_data_db
    staging_name = rt.registry.staging_collection_name("smd")
    loaded_name = rt.registry.public_data_collection_name("smd")
    src_db = staging_db_name(rt.registry, "nucc")
    src_coll_name = staging_collection_name(rt.registry, "nucc")
    current_hash = _current_nucc_source_hash(ctx)

    # Universal skip gate (pipeline_loaded_metadata.should_skip):
    #   * hash matches metadata.source_hash (metadata on frontend cluster)
    #   * metadata.operationally_fit is True
    #   * loaded PipelinePublicHealthData collection exists on pipeline cluster
    #   * row count on collection == metadata.row_count
    # All four true -> no reload needed; return skip summary.
    skip, reason = should_skip(
        pipeline_mongo=rt.mongo,
        frontend_mongo=rt.frontend,
        registry=rt.registry,
        source_name="smd",
        publichealthdata_collection_name=loaded_name,
        current_source_hash=current_hash,
    )
    if skip:
        return {
            "skipped": True,
            "reason": reason,
            "publichealthdata_collection_name": loaded_name,
            "source_hash": current_hash,
        }

    # Fail fast if the OpenAI key is missing — better here than after
    # the copy step has already staged rows.
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        _bail_openai_key_missing()

    # Source: NUCC staging on the pipeline cluster (populated by
    # normalize_nucc). Read the run_id filter so a residual older run's
    # rows never sneak into the publish.
    src = rt.mongo[src_db][src_coll_name]

    # Target: pipeline cluster. Staging + loaded DB names come from the
    # registry (dataset_versions[]). Cross-DB atomic swap happens at the
    # end via `client.admin.command('renameCollection', ..., dropTarget=True)`
    # (the admin command supports cross-DB; only the Collection.rename()
    # pymongo helper is same-DB-only).
    smd_staging = rt.mongo[staging_db][staging_name]

    # Wipe any residue from a previous partially-completed run before
    # writing this run's rows. dropTarget=True on the final rename would
    # handle live's residue; staging is our own scratch space and a stray
    # earlier row would inflate the publish count.
    smd_staging.delete_many({})

    copied = 0
    batch: list[dict] = []
    for row in src.find({"run_id": rt.run_id}):
        pub = {
            k: v
            for k, v in row.items()
            if k not in ("_id", "_source_row_index", "raw")
        }
        # Ensure run_id is set on every published row. Same field name
        # provider records use; matches _loaded_metadata.run_id so an
        # operator can trace any row back to its metadata doc.
        pub["run_id"] = rt.run_id
        batch.append(pub)
        if len(batch) >= 500:
            smd_staging.insert_many(batch, ordered=False)
            copied += len(batch)
            batch = []
    if batch:
        smd_staging.insert_many(batch, ordered=False)
        copied += len(batch)

    # Embed every staged row on the pipeline cluster. generate_specialty
    # _embeddings iterates find({}) on the collection we point it at, so
    # restricting to the SMD staging collection keeps scope tight.
    embed_summary = generate_specialty_embeddings(
        {
            "specialty_collection": f"{staging_db}.{staging_name}",
            "openai_api_key": os.environ.get("OPENAI_API_KEY"),
        },
        mongo=rt.mongo,
    )

    # Non-fatal: record one discrepancy per SMD row still missing an
    # embedding vector after generate_specialty_embeddings returned.
    # Embed batches that failed the OpenAI call surface here as un-
    # embedded rows; we publish SMD anyway (SMD is usable for lookups
    # without embeddings; FindCare $vectorSearch degrades gracefully)
    # AND write per-row discrepancies so the PDF report + operator see
    # exactly which codes need re-embedding when the API is available
    # again. Reason prefixed 'error_' per operator directive 2026-08-02
    # -- non-fatal but still an error class the PDF report elevates.
    unembedded_count = 0
    for row in smd_staging.find(
        {"embedding": {"$exists": False}},
        {"Code": 1, "Display Name": 1, "is_supplemented": 1},
    ):
        rt.record_discrepancy(
            npi=None,
            reason="error_specialty_embedding_failed",
            step="publish_smd_and_embed",
            entity_kind="specialty",
            detail={
                "code": row.get("Code"),
                "display_name": row.get("Display Name"),
                "is_supplemented": bool(row.get("is_supplemented")),
                "note": (
                    "OpenAI embedding call did not populate this SMD row; "
                    "row published without embedding vector. Re-run "
                    "publish_smd_and_embed after OpenAI credits are "
                    "topped to backfill."
                ),
            },
        )
        unembedded_count += 1

    # Atomic swap ACROSS databases. renameCollection via admin.command
    # supports cross-DB source/target (the pymongo Collection.rename()
    # helper does NOT -- that one is same-DB only). dropTarget=True
    # drops any prior loaded collection in the same server-side op, so
    # consumers transition from prior-fire-complete to this-fire-complete
    # atomically.
    rt.mongo.admin.command({
        "renameCollection": f"{staging_db}.{staging_name}",
        "to": f"{loaded_db}.{loaded_name}",
        "dropTarget": True,
    })

    # Mark loaded per operator rule: non-fatal errors (like embed 429s
    # that produced discrepancies) still mark the collection loaded and
    # operationally_fit. A fatal error earlier in this step would have
    # raised before reaching this point, leaving no metadata doc, which
    # is the signal for the next fire to reload. Metadata lives on the
    # frontend cluster (chathealthyfrontend.pipeline.loaded_metadata).
    loaded_row_count = rt.mongo[loaded_db][loaded_name].count_documents({})
    mark_loaded(
        frontend_mongo=rt.frontend,
        publichealthdata_collection_name=loaded_name,
        staging_collection_name=staging_name,
        source_hash=current_hash,
        run_id=rt.run_id,
        data_version=dv,
        row_count=loaded_row_count,
        operationally_fit=True,
        detail={
            "embed_candidates": embed_summary["candidate_count"],
            "embed_updated": embed_summary["updated_count"],
            "embed_failed": embed_summary["failed_count"],
            "embed_unembedded_rows": unembedded_count,
            "embed_model": embed_summary["model"],
            "embed_dimensions": embed_summary["dimensions"],
        },
    )

    return {
        "skipped": False,
        "publichealthdata_collection_name": loaded_name,
        "smd_collection": f"{loaded_db}.{loaded_name}",
        "rows_copied": copied,
        "row_count": loaded_row_count,
        "source_hash": current_hash,
        "embed_candidates": embed_summary["candidate_count"],
        "embed_updated": embed_summary["updated_count"],
        "embed_failed": embed_summary["failed_count"],
        "embed_unembedded_rows": unembedded_count,
        "embed_model": embed_summary["model"],
        "embed_dimensions": embed_summary["dimensions"],
    }
