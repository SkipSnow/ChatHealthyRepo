# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""medicare_fetch_all_sources (EPIC-010-F-007) — one worker per Medicare source.

Process-pool fanned by source_name_base. The work-item assignment carries ONLY
the source_name key (partition = {"source": <source_name>}); it carries no
parameters. The worker is a pure robot with no logic of its own: it reads that
key, looks the key up in the persistent config to get the prefabricated,
model-free fetch_spec (the data-fetch agent's typed find input), builds a
FetchRequest from those params, and calls the agent's run(). It computes,
discovers and manufactures nothing. There is no url-discovery and no
CH_URL_DISCOVERY_MODEL on this path: a deterministic fetch_spec needs no model.

The agent streams the bytes straight to the run's transient blob target -- the
same {env}-pipeline-transients container, keyed {run_id}/{source}/..., that the
downstream medicare_source_archival and medicare_load_staging steps read. When
the source's config carries an extract spec, the fetch itself pulls one member
out of the downloaded archive (descending nested zips) and stores that member,
so each source that lives inside a shared archive fetches and extracts its own
table independently -- no cross-source bundle extraction in this step.
"""

from __future__ import annotations

import os

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle import data_fetch_agent as _dfa
from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime

_log = ChatHealthyLoggingService()

_FETCH_BUFFER_BYTES = 1024 * 1024
_TRANSIENT_SUFFIX = "-pipeline-transients"


def _require_source(partition: dict):
    """Raise-only helper: the catcher logs, not the thrower."""
    source = partition.get("source") if isinstance(partition, dict) else None
    if not source:
        raise ChatHealthyException(
            mode="runtime_error",
            component="medicare_fetch_all_sources",
            message=("medicare_fetch_all_sources: expected "
                     "ctx.config['partition']['source'] to name the source key "
                     "this worker owns"),
            partition=repr(partition))
    return source


def _require_connection_string():
    """Raise-only helper: the catcher logs, not the thrower."""
    conn = os.environ.get("PIPELINE_STORAGE_CONNECTION_STRING", "").strip()
    if not conn:
        raise ChatHealthyException(
            mode="config_error",
            component="medicare_fetch_all_sources",
            message=("medicare_fetch_all_sources: PIPELINE_STORAGE_CONNECTION_STRING "
                     "is not set; the fetch has no blob target to stream to."))
    return conn


def _record_from_result(source: str, res, container: str, run_id: str,
                        env_prefix: str) -> dict:
    """The per-source fetch_result the downstream steps read: blob coordinates,
    content hash, size and version identity. Shape matches what the shared
    source_fetch_engine produced, so archival and load_staging are unchanged."""
    stored = res.stored or {}
    return {
        "source_name": source,
        "run_id": run_id,
        "env": env_prefix,
        "blob_container": stored.get("container") or container,
        "blob_path": stored.get("blob_path"),
        "filename": res.filename,
        "sha256": res.sha256,
        "size_bytes": res.size_bytes,
        "version": (res.sha256[:16] if res.sha256 else None),
        "source_version_identifier": res.version_identifier,
        "url": res.url,
        "skipped": False,
    }


def run_step(ctx) -> dict:
    partition = ctx.config.get("partition") or {}
    source = _require_source(partition)

    rt = PipelineRuntime(ctx)
    reg = rt.registry

    # Pure lookup: KEY -> config -> params. The worker manufactures nothing.
    spec = reg.fetch_spec(source)
    extract = reg.fetch_extract(source)
    source_encoding = reg.fetch_source_encoding(source)

    conn = _require_connection_string()
    container = f"{ctx.env_prefix}{_TRANSIENT_SUFFIX}"
    blob_path = f"{ctx.run_id}/{source}/{source}"

    _log.LogPipeline("INFO",
        "medicare_fetch_all_sources: start source=%s run_id=%s mode=%s extract=%s "
        "target=%s/%s",
        source, ctx.run_id, spec.get("mode"),
        (extract.get("member_path") if extract else None), container, blob_path)

    request = _dfa.FetchRequest(
        source_name=source,
        find=spec,
        store=_dfa.BlobStore(connection_string=conn, container=container,
                             blob_path=blob_path),
        stream=True,
        buffer_size=_FETCH_BUFFER_BYTES,
        extract=extract,
        source_encoding=source_encoding,
    )
    res = _dfa.run(request)

    record = _record_from_result(source, res, container, ctx.run_id, ctx.env_prefix)
    ctx.manifest.source_versions[source] = record["version"]
    fetch_results = ctx.manifest.metrics.setdefault("fetch_results", {})
    fetch_results[source] = record
    _log.LogPipeline("INFO",
        "medicare_fetch_all_sources: fetched source=%s run_id=%s url=%s bytes=%d",
        source, ctx.run_id, res.url, res.size_bytes or 0)

    return {"source": source, "version": record["version"],
            "size_bytes": res.size_bytes}


def execute(ctx):
    return run_step(ctx)
