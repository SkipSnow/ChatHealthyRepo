# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Publish PublicHealthData.Provider_staging_v_N -> Provider_v_N + mark loaded.

Operator directive 2026-08-02: consumers must never observe partially-
enriched Provider records mid-pipeline. Every Provider write step targets
PublicHealthData.Provider_staging_v_{data_version} throughout the run.
This step is the atomic transition from prior-fire-complete to
this-fire-complete: renameCollection(dropTarget=true) swaps the staging
collection into PublicHealthData.Provider_v_{data_version}, and then
pipeline_loaded_metadata.mark_loaded records the load-state.

Runs late in the DAG (after every Provider enrichment step and the
post_load_reconciliation gate), so the only way it fires is if every
upstream Provider write step completed non-fatally.

Same-DB requirement of renameCollection is why both staging + loaded
live in PublicHealthData.

State-scoped runs (state_scope != ALL) still write to staging as usual;
the swap replaces the loaded collection wholesale with only the states
this run built. That means running a single-state fire against a
production loaded collection would drop every other state. For test
fires against dev this is fine; production fires MUST always be
state_scope=ALL. The Atlas cluster reservation semaphore prevents two
overlapping fires, so cross-run collision on the swap is impossible.
"""

from __future__ import annotations

import os

from chathealthy_frontend_lib.exceptions import ChatHealthyException

from pipeline_loaded_metadata import mark_loaded
from pipeline_runtime import PipelineRuntime


_STAGING_DB = "PublicStaging"
_LOADED_DB = "PublicHealthData"


def _loaded_coll_name(data_version: int) -> str:
    return f"Provider_v_{data_version}"


def _staging_coll_name(data_version: int) -> str:
    return f"Provider_staging_v_{data_version}"


def _current_nppes_source_hash(ctx) -> str:
    """Provider's primary source is the NPPES dissemination file. Read
    the sha256 fetch_all_sources stamped on this run's metrics."""
    manifest = getattr(ctx, "manifest", None)
    metrics = (getattr(manifest, "metrics", None) or {}) if manifest else {}
    fetch = metrics.get("fetch_results") or {}
    nppes = fetch.get("nppes_npi") or {}
    return (nppes.get("sha256") or nppes.get("source_version_identifier") or "").strip()


def _bail_staging_missing(coll_name: str) -> None:
    raise ChatHealthyException(
        mode="runtime_error",
        message=(
            f"publish_provider: PublicStaging.{coll_name} does not exist. "
            "No Provider staging collection to publish -- upstream write "
            "steps must have failed or been skipped."
        ),
    )


def execute(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    dv = rt.data_version
    loaded_name = _loaded_coll_name(dv)
    staging_name = _staging_coll_name(dv)

    staging_db_h = rt.mongo[_STAGING_DB]
    if staging_name not in staging_db_h.list_collection_names():
        _bail_staging_missing(staging_name)

    staging_coll = staging_db_h[staging_name]
    staging_row_count = staging_coll.count_documents({})

    # Atomic swap ACROSS databases: PublicStaging.<staging_name> ->
    # PublicHealthData.<loaded_name>. admin.command('renameCollection', ...)
    # supports cross-DB (Collection.rename() is same-DB only). dropTarget
    # drops any prior loaded collection in the same server-side op, so
    # consumers transition prior-fire-complete -> this-fire-complete
    # atomically.
    rt.mongo.admin.command({
        "renameCollection": f"{_STAGING_DB}.{staging_name}",
        "to": f"{_LOADED_DB}.{loaded_name}",
        "dropTarget": True,
    })

    loaded_row_count = rt.mongo[_LOADED_DB][loaded_name].count_documents({})

    # Mark loaded on the frontend cluster. Absence of this call = the
    # swap didn't happen (fatal error), and pipeline.loaded_metadata
    # retains its prior entry which is what the next fire's should_skip
    # compares against.
    mark_loaded(
        frontend_mongo=rt.frontend,
        publichealthdata_collection_name=loaded_name,
        staging_collection_name=staging_name,
        source_hash=_current_nppes_source_hash(ctx),
        run_id=rt.run_id,
        data_version=dv,
        row_count=loaded_row_count,
        operationally_fit=True,
    )

    return {
        "publichealthdata_collection_name": loaded_name,
        "staging_collection_name": staging_name,
        "staging_row_count": staging_row_count,
        "loaded_row_count": loaded_row_count,
    }
