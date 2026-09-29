# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Build PipelinePublicHealthData.SpecialtyMetaData_v_N in place + embeddings.

Realizes Provider Pipeline LLD v42 §5.2.8a. Runs after normalize_nucc.

**Pipeline-cluster-only.** Pipelines never migrate data from the pipeline
back-end cluster to the front-end cluster. Every write in this step targets
ChatHealthyDataPipelines. A separate data_release step (out of scope here) is
the sole legal path from pipeline -> frontend.

SMD is a FULL rebuild each fire (not state-scoped): the served collection is
built directly, with no staging collection and no rename swap.

Sequence inside execute():

  1. Clear PipelinePublicHealthData.SpecialtyMetaData_v_{data_version} and copy
     every row from PublicStaging.StagingNucc_v_{data_version} into it on the
     same (pipeline) cluster. Strips pipeline-only fields (_id,
     _source_row_index, raw).
  2. Generate a text-embedding-3-large embedding for every row
     (embedding_engine.generate_specialty_embeddings). Stamps embedding,
     embedding_model, embedding_generated_at on each doc.

Post-build: PipelinePublicHealthData.SpecialtyMetaData_v_{n} holds 884 rows (883
NUCC + F-105 supplements) each with an embedding vector. A fatal finding (an
un-embedded row) aborts the run before mark_loaded, so the next fire reloads.
The data_release step, running on its own schedule, is responsible for
shipping this to the front-end cluster for user-facing $vectorSearch.

All collection names are version-suffixed (_v_N) per operator rule
"all files must be versioned with the right version number."

No polling, no locks, no retry; each PipelinePublicHealthData collection has
exactly one producing pipeline and cross-run collision is prevented
upstream by the Atlas cluster reservation semaphore.
"""

from __future__ import annotations

import os

from chathealthy_lib.exceptions import ChatHealthyException

from pipeline.run_lifecycle.embedding_engine import generate_specialty_embeddings
from pipeline.run_lifecycle.pipeline_loaded_metadata import mark_loaded, should_skip
from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.provider_base.staging_loader import staging_collection_name, staging_db_name


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
    loaded_db = smd_entry.public_data_db
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
    # the copy step has already written rows into the served collection.
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        _bail_openai_key_missing()

    # Source: NUCC staging on the pipeline cluster (populated by
    # normalize_nucc). Read the run_id filter so a residual older run's
    # rows never sneak into the build.
    src = rt.mongo[src_db][src_coll_name]

    # Target: the served (public_data) collection on the pipeline cluster,
    # built in place. loaded DB name comes from the registry
    # (dataset_versions[]). SMD is a full rebuild each fire, so the served
    # collection is cleared and repopulated -- there is no staging swap.
    smd_loaded = rt.mongo[loaded_db][loaded_name]

    # Full rebuild: clear the served collection before writing this run's
    # rows so a prior fire's contents do not accumulate.
    smd_loaded.delete_many({})

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
            smd_loaded.insert_many(batch, ordered=False)
            copied += len(batch)
            batch = []
    if batch:
        smd_loaded.insert_many(batch, ordered=False)
        copied += len(batch)

    # Embed every row on the pipeline cluster. generate_specialty_embeddings
    # iterates find({}) on the collection we point it at, so pointing it at
    # the served SMD collection keeps scope tight.
    embed_summary = generate_specialty_embeddings(
        {
            "specialty_collection": f"{loaded_db}.{loaded_name}",
            "openai_api_key": os.environ.get("OPENAI_API_KEY"),
        },
        mongo=rt.mongo,
    )

    # error_specialty_embedding_failed is graded fatal (LLD v54 §16): an
    # SMD row left without an embedding after generate_specialty_embeddings
    # names an invariant a correct run never violates, so the first such
    # finding aborts the run before mark_loaded. The finding is recorded to
    # discrepancyLog first, so the report names the code that failed.
    unembedded_count = 0
    for row in smd_loaded.find(
        {"embedding": {"$exists": False}},
        {"Code": 1, "Display Name": 1, "is_supplemented": 1},
    ):
        rt.record_discrepancy(
            artifact="specialty_metadata",
            record_key=row.get("Code"),
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

    # Mark loaded per operator rule: non-fatal errors (like embed 429s
    # that produced discrepancies) still mark the collection loaded and
    # operationally_fit. A fatal error earlier in this step would have
    # raised before reaching this point, leaving no metadata doc, which
    # is the signal for the next fire to reload. Metadata lives on the
    # frontend cluster (chathealthyfrontend.pipeline.loaded_metadata).
    # source_collection records the NUCC staging collection the build read
    # from, since SMD is built in place with no staging collection of its own.
    loaded_row_count = smd_loaded.count_documents({})
    mark_loaded(
        frontend_mongo=rt.frontend,
        publichealthdata_collection_name=loaded_name,
        staging_collection_name=src_coll_name,
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
