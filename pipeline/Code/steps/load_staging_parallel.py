# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""load_staging_parallel — Provider Pipeline step wrapper.

One Worker per source. The step is dispatched with partition_key=
"source_name" so 6 Workers run concurrently (NPPES + 5 reference/
derived sources). Each Worker loads its ONE source into the versioned
staging collection on the pipeline cluster (ChatHealthyPipelines.
PublicStaging.<Base>_v_{data_version}).

NPPES-specific behavior (applied by the loader; this handler just passes
the args through):
  * Rows outside ctx.args.resolved_states() are skipped.
  * On full-load mode, prior rows in the versioned collection whose
    state is in scope are deleted BEFORE insert (so re-runs of the same
    state scope don't accumulate).
  * Unique index on npi — duplicate insert => BulkWriteError => step
    fails loud. No retry, no fallback.
"""

from __future__ import annotations
from chathealthy_frontend_lib.exceptions import ChatHealthyException
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


from staging_loader import load_staging

_log = ChatHealthyLoggingService()


# Per-source format spec. Third slot is the CSV delimiter when applicable
# (default comma), or None when not applicable. NPPES uses tab; the census
# ZCTA/county file uses pipe. usda_rucc is an Excel .xlsx binary.
_FORMAT_BY_SOURCE = {
    "nppes_npi": ("zip_csv", "npidata", None),
    "pl_pfile": ("zip_csv", "pl_pfile", None),
    "nucc": ("csv", None, ","),
    "census_zcta_county": ("csv", None, "|"),
    "usda_rucc": ("xlsx", None, None),
    "specialty_catalog": ("json", None, None),
}


def _require_partition_source(partition) -> str:
    if not isinstance(partition, dict) or not partition.get("source"):
        raise ChatHealthyException(
            mode="value_error",
            message=(
                "load_staging_parallel: expected ctx.config['partition']"
                "['source'] to name the source this worker owns. Step is "
                "partition_key='source_name'; Controller assigns one "
                "source per Worker."
            ),
            partition=repr(partition),
        )
    return partition["source"]


def _require_fetch_result(source_name: str, fetch_results: dict) -> dict:
    r = fetch_results.get(source_name)
    if not r:
        raise ChatHealthyException(
            mode="value_error",
            message=f"load_staging_parallel[{source_name}]: no fetch_result on manifest",
            source_name=source_name,
        )
    if r.get("skipped"):
        raise ChatHealthyException(
            mode="value_error",
            message=(
                f"load_staging_parallel[{source_name}]: fetch_result marks "
                "skipped=true. Cannot load a skipped source. If skip is "
                "intentional, remove the source from the partition list."
            ),
            source_name=source_name,
        )
    return r


def _require_format(source_name: str) -> tuple[str, str | None, str | None]:
    fmt_map = _FORMAT_BY_SOURCE.get(source_name)
    if not fmt_map:
        raise ChatHealthyException(
            mode="value_error",
            message=f"load_staging_parallel: no format mapping for source {source_name!r}",
            source_name=source_name,
        )
    return fmt_map


def run_step(ctx) -> dict:
    """Worker path: load ONE source's staging data, identified by
    ctx.config['partition']['source']."""
    partition = ctx.config.get("partition") or {}
    source_name = _require_partition_source(partition)

    fetch_results = ctx.manifest.metrics.get("fetch_results") or {}
    fetch_result = _require_fetch_result(source_name, fetch_results)
    fmt, hint, delimiter = _require_format(source_name)

    spec = {
        "blob_container": fetch_result.get("blob_container"),
        "blob_path": fetch_result.get("blob_path"),
        "format": fmt,
    }
    if hint:
        spec["inner_name_hint"] = hint
    if delimiter:
        spec["delimiter"] = delimiter

    config = dict(ctx.config)
    config.setdefault("run_id", ctx.run_id)
    config.setdefault("env", ctx.env_prefix)
    config["sources"] = {source_name: spec}
    config["states"] = ctx.args.resolved_states()
    config["incremental"] = bool(ctx.args.incremental)
    config["data_version"] = int(ctx.args.data_version)

    result = load_staging(
        config,
        mongo=ctx.mongo_client,
        blob=ctx.blob_client,
    ) or {}
    _log.info("load_staging_parallel[%s]: done result=%s", source_name, result)
    return result


def execute(ctx):
    return run_step(ctx)
