# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""medicare_fetch_all_sources (EPIC-010-F-007) — one worker per Medicare source.

Process-pool fanned by source_name_base: each worker fetches the one source it
owns and lands it to the transient container, driving the shared
source_fetch_engine. The source URL is resolved from the dataset_versions[]
fetch block through the registry (which runs the url-discovery worker when the
entry declares a page_url + instructions), so acquisition stays declarative.
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.provider_base.source_fetch_engine import fetch_all_sources as _engine_fetch
from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime

_log = ChatHealthyLoggingService()


def _apply_freshness(spec: dict, fresh_val) -> dict:
    if isinstance(fresh_val, dict):
        spec.setdefault("freshness_decision", fresh_val.get("decision", "fetch"))
        for k in ("archive_container", "archive_blob", "archive_filename", "archived_version"):
            if fresh_val.get(k) is not None:
                spec[k] = fresh_val[k]
    elif fresh_val:
        spec.setdefault("freshness_decision", fresh_val)
    return spec


def run_step(ctx) -> dict:
    partition = ctx.config.get("partition") or {}
    source = partition.get("source") if isinstance(partition, dict) else None
    if not source:
        raise ChatHealthyException(
            mode="runtime_error",
            message="medicare_fetch_all_sources: expected ctx.config['partition']['source'] to name the source this worker owns",
            component="medicare_fetch_all_sources",
            partition=partition,
        )

    rt = PipelineRuntime(ctx)
    reg = rt.registry
    # Resolves a direct source_url, or runs the url-discovery worker when the
    # dataset_versions fetch block declares page_url + instructions.
    url = rt.registry.resolve_source_url(source)

    freshness = ctx.manifest.metrics.get("source_freshness") or {}
    spec = _apply_freshness({"source_url": url}, freshness.get(source))
    sources = {source: spec}

    # Bundled sources: this owner's worker also extracts every bundle member it
    # does not own (the Coverage DB ICD-10 table from the HCPCS owner's zip),
    # keyed by the member's archive_member as the zip-entry glob -- the member
    # cannot run as its own worker (the owner's blob is process-local).
    entry = reg.by_source_name(source)
    if entry.is_bundled and entry.is_fetched:
        for member in reg.bundle_members(entry.bundled_with):
            if member.source_name == source or member.is_fetched:
                continue
            child = {"derived_from": source, "zip_entry_glob": member.archive_member}
            sources[member.source_name] = _apply_freshness(child, freshness.get(member.source_name))

    config = {
        "run_id": ctx.run_id,
        "env": ctx.env_prefix,
        "transient_container": f"{ctx.env_prefix}-pipeline-transients",
        "sources": sources,
    }
    result = _engine_fetch(config, mongo=ctx.mongo_client, blob=ctx.blob_client) or {}

    ctx.manifest.source_versions.update(result.get("source_versions") or {})
    fetch_results = ctx.manifest.metrics.setdefault("fetch_results", {})
    for r in result.get("results") or []:
        fetch_results[r["source_name"]] = r
    _log.LogPipeline("INFO", "medicare_fetch_all_sources: fetched source=%s run_id=%s", source, ctx.run_id)
    return {"source": source, **result}


def execute(ctx):
    return run_step(ctx)
