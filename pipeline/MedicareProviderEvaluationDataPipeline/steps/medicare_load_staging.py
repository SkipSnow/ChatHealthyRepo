# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""medicare_load_staging (EPIC-010-F-007) — land each source row to staging.

Process-pool fanned by source_name: each worker loads the one source it owns
into its versioned staging collection, driving the shared staging_loader. The
loader token and inner-file hint are derived from the source's declared
file_format and archive_member in dataset_versions[] (declarative, via the
registry) and the blob coordinates from the fetch result on the manifest.
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.provider_base.staging_loader import load_staging
from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime

_log = ChatHealthyLoggingService()

# Declared config file_format -> (loader iterator token, csv delimiter).
# Mirrors the conventional loader's _resolve_iter token vocabulary; delimited
# text variants ride the csv reader with the right delimiter.
_FORMAT_TOKEN = {
    "csv": ("csv", ","),
    "tsv": ("csv", "\t"),
    "pipe_delimited": ("csv", "|"),
    "xlsx": ("xlsx", None),
    "json": ("json", None),
    "zip_containing_csv": ("zip_csv", None),
    "zip_containing_json": ("zip_json", None),
    "zip_containing_icd10cm_order": ("zip_icd10cm_order", None),
}


def run_step(ctx) -> dict:
    partition = ctx.config.get("partition") or {}
    source = partition.get("source") if isinstance(partition, dict) else None
    if not source:
        raise ChatHealthyException(
            mode="value_error",
            message="medicare_load_staging: expected ctx.config['partition']['source'] to name the source this worker owns",
            component="medicare_load_staging",
            partition=repr(partition),
        )

    rt = PipelineRuntime(ctx)
    entry = rt.registry.by_source_name(source)
    if not entry.file_format:
        raise ChatHealthyException(
            mode="value_error",
            message=f"medicare_load_staging[{source}]: dataset entry has no file_format to load",
            component="medicare_load_staging",
            source_name=source,
        )
    token = _FORMAT_TOKEN.get(entry.file_format)
    if token is None:
        raise ChatHealthyException(
            mode="value_error",
            message=f"medicare_load_staging[{source}]: unsupported file_format {entry.file_format!r}",
            component="medicare_load_staging",
            source_name=source,
        )
    base_fmt, delimiter = token

    # Bundled sources resolve the table differently: the fetch owner still
    # carries the whole downloaded zip, so its table is extracted at load
    # (zip_csv + the archive_member hint); a bundled extractor was already
    # pulled out flat at fetch, so it loads as a plain delimited file.
    inner_hint = entry.archive_member
    if entry.is_bundled and entry.is_fetched:
        fmt = "zip_csv"
    elif entry.is_bundled:
        fmt = "csv" if base_fmt == "zip_csv" else base_fmt
        inner_hint = None
    else:
        fmt = base_fmt

    fetch_results = ctx.manifest.metrics.get("fetch_results") or {}
    fr = fetch_results.get(source)
    if not fr or not fr.get("blob_container") or not fr.get("blob_path"):
        raise ChatHealthyException(
            mode="value_error",
            message=(
                f"medicare_load_staging[{source}]: no fetch_result with "
                f"blob_container/blob_path on the manifest; fetch must run first"
            ),
            component="medicare_load_staging",
            source_name=source,
        )

    spec = {
        "blob_container": fr.get("blob_container"),
        "blob_path": fr.get("blob_path"),
        "format": fmt,
    }
    if inner_hint:
        spec["inner_name_hint"] = inner_hint
    if delimiter and delimiter != ",":
        spec["delimiter"] = delimiter

    config = dict(ctx.config)
    config.setdefault("run_id", ctx.run_id)
    config.setdefault("env", ctx.env_prefix)
    config["sources"] = {source: spec}
    config["states"] = ctx.args.staging_states()
    config["incremental"] = bool(ctx.args.incremental)
    config["data_version"] = int(ctx.args.data_version)

    result = load_staging(config, mongo=ctx.mongo_client, blob=ctx.blob_client) or {}
    _log.LogPipeline("INFO", "medicare_load_staging[%s]: done result=%s", source, result)
    return result


def execute(ctx):
    return run_step(ctx)
