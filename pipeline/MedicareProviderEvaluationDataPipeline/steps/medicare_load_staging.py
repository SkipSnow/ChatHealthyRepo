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
    "rrf_rxnconso": ("rrf_rxnconso", None),
    "rrf_rxnrel": ("rrf_rxnrel", None),
}


# The indexes each staged source needs, created by the step that creates the
# collection. They cannot be declared by the deployment: the collection does
# not exist when the chain deploys (operator ruling 2026-10-24).
#
#   prscrbr_state__id -- the base fans out on the row's own CMS state and
#       pages by _id within it; without this, each of the 61 state workers
#       scans all 28M rows.
#   molecule_id__id -- the specialty rollup fans out on the molecule and pages
#       by _id within its slice, so each worker reads only the rows of the
#       molecules it owns.
#   tty -- the identity ladder reads the ingredient, brand and substance rows
#       out of 1.2M RXNCONSO rows.
#   rela -- the ladder follows only the relationships that reach an
#       ingredient, out of 7.5M RXNREL rows.
STAGING_INDEXES: dict[str, list[dict]] = {
    "medicare_partd": [
        {"keys": [("raw.Prscrbr_State_Abrvtn", 1), ("_id", 1)],
         "name": "prscrbr_state__id"},
        {"keys": [("molecule_id", 1), ("_id", 1)], "name": "molecule_id__id"},
    ],
    "rxnorm": [
        {"keys": [("raw.TTY", 1)], "name": "tty"},
    ],
    "rxnorm_rel": [
        {"keys": [("raw.RELA", 1)], "name": "rela"},
    ],
    "openfda_labels": [
        # index_fda_prescription_records and build_fda_label_resolution both
        # query by fda_record_number and carry no run_id, by design: a
        # drained-and-replaced staging is the one truth. The existing
        # (run_id, fda_record_number) index leads with run_id, so it cannot
        # serve a query that does not name one.
        {"keys": [("fda_record_number", 1)], "name": "fda_record_number_1"},
    ],
}


def _create_indexes(ctx, rt, source: str) -> list[str]:
    """Create this source's staging indexes. Idempotent: an index that is
    already there is not rebuilt."""
    specs = STAGING_INDEXES.get(source)
    if not specs:
        return []
    reg = rt.registry
    entry = reg.by_source_name(source)
    coll = ctx.mongo_client[entry.staging_db][
        reg.staging_collection_name(source)]
    return [coll.create_index(s["keys"], name=s["name"]) for s in specs]


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
    blob_paths = fr.get("blob_paths") if fr else None
    if not fr or not fr.get("blob_container") or not (fr.get("blob_path") or blob_paths):
        raise ChatHealthyException(
            mode="value_error",
            message=(
                f"medicare_load_staging[{source}]: no fetch_result with "
                f"blob_container and blob_path(s) on the manifest; fetch must run first"
            ),
            component="medicare_load_staging",
            source_name=source,
        )

    spec = {
        "blob_container": fr.get("blob_container"),
        "format": fmt,
    }
    # A sharded source (openFDA drug/label) lands as many shard blobs; pass the
    # whole list so the loader's sharded path ingests them under one drop.
    # Every other source is a single blob.
    if blob_paths:
        spec["blob_paths"] = blob_paths
    else:
        spec["blob_path"] = fr.get("blob_path")
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
    made = _create_indexes(ctx, rt, source)
    if made:
        result["indexes_created"] = made
    _log.LogPipeline("INFO", "medicare_load_staging[%s]: done result=%s", source, result)
    return result


def execute(ctx):
    return run_step(ctx)
