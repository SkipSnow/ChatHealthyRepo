# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Publish StagingNucc -> SpecialtyMetaData + generate embeddings.

Realizes Provider Pipeline LLD v42 §5.2.8a. Runs after normalize_nucc.

Sequence inside execute():

  1. Copy every row from PublicStaging.StagingNucc_v_{data_version} on
     the pipeline cluster into a staging collection on the frontend
     cluster: {env}_PublicHealthData.SpecialtyMetaData_staging. Strips
     pipeline-only fields (_id, run_id, _source_row_index, raw).
  2. Generate a text-embedding-3-large embedding for every row in the
     frontend-side staging collection (embedding_engine
     .generate_specialty_embeddings). Stamps embedding, embedding_model,
     embedding_generated_at on each doc.
  3. Atomic swap on the frontend cluster:
        SpecialtyMetaData_staging  -->  SpecialtyMetaData
     via Collection.rename(new_name, dropTarget=True). Both collections
     live in {env}_PublicHealthData so the rename is same-DB.

After a successful run the front-end $vectorSearch (EPIC-002-F-005-S-001
-REQ-T-003) sees a fully-embedded SMD with 883 NUCC + F-105 supplements.
FindCare's specialty filter can execute without falling back on regex.

No polling, no locks, no retry; each PublicData collection has exactly
one producing pipeline and cross-run collision is prevented upstream by
the Atlas cluster reservation semaphore (v42 §5.2.8a bullet 8).
"""

from __future__ import annotations

import os

from chathealthy_frontend_lib.exceptions import ChatHealthyException

from embedding_engine import generate_specialty_embeddings
from pipeline_runtime import PipelineRuntime
from staging_loader import STAGING_DB_NAME, staging_collection_name


_LIVE_COLLECTION = "SpecialtyMetaData"
_STAGING_COLLECTION = "SpecialtyMetaData_staging"


def _smd_db_name(env: str) -> str:
    return f"{env}_PublicHealthData"


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

    # Fail fast if the OpenAI key is missing — better here than after
    # the copy step has already touched the frontend cluster.
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        _bail_openai_key_missing()

    # Source: StagingNucc on the pipeline cluster (populated by
    # normalize_nucc). Read the run_id filter so a residual older run's
    # rows never sneak into the publish.
    src_coll_name = staging_collection_name("nucc", rt.data_version)
    src = rt.mongo[STAGING_DB_NAME][src_coll_name]

    smd_db = rt.frontend[_smd_db_name(rt.env)]
    smd_staging = smd_db[_STAGING_COLLECTION]

    # Wipe any residue from a previous partially-completed run before
    # writing this run's rows. dropTarget=True on the final rename would
    # handle live's residue; SMD_staging is our own scratch space and
    # a stray earlier row would inflate the publish count.
    smd_staging.delete_many({})

    copied = 0
    batch: list[dict] = []
    for row in src.find({"run_id": rt.run_id}):
        pub = {
            k: v
            for k, v in row.items()
            if k not in ("_id", "run_id", "_source_row_index", "raw")
        }
        batch.append(pub)
        if len(batch) >= 500:
            smd_staging.insert_many(batch, ordered=False)
            copied += len(batch)
            batch = []
    if batch:
        smd_staging.insert_many(batch, ordered=False)
        copied += len(batch)

    # Embed every staged row. generate_specialty_embeddings iterates
    # find({}) on the collection we point it at, so restricting to
    # _STAGING_COLLECTION keeps the embedding scope tight.
    embed_summary = generate_specialty_embeddings(
        {
            "specialty_collection": f"{_smd_db_name(rt.env)}.{_STAGING_COLLECTION}",
            "openai_api_key": os.environ.get("OPENAI_API_KEY"),
        },
        mongo=rt.frontend,
    )

    # Atomic swap. rename(dropTarget=True) on the SAME database is the
    # server-side MongoDB renameCollection admin command, executed as
    # one op. Post-swap: the live SpecialtyMetaData collection is what
    # SMD_staging held; the old live is dropped in the same command.
    smd_staging.rename(_LIVE_COLLECTION, dropTarget=True)

    return {
        "smd_collection": f"{_smd_db_name(rt.env)}.{_LIVE_COLLECTION}",
        "rows_copied": copied,
        "embed_candidates": embed_summary["candidate_count"],
        "embed_updated": embed_summary["updated_count"],
        "embed_failed": embed_summary["failed_count"],
        "embed_model": embed_summary["model"],
        "embed_dimensions": embed_summary["dimensions"],
    }
