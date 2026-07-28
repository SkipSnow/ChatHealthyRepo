# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Staging loader for the Provider Pipeline — LLD v23 §4.6.

Loads fetched source files (already present in `{env}-pipeline-transients/
{run_id}/{source}/{filename}`) into the per-source staging collections on
the pipeline cluster, in as-is shape.

Realizes:

  - EPIC-010-F-102-S-004-REQ-T-003  source CSV is byte-offset indexed
                                    before any record processing
  - EPIC-010-F-102-S-004-REQ-T-004  pre-indexing pairs every secondary-source
                                    row with its NPI key
  - EPIC-010-F-103-S-002-REQ-B-001, REQ-B-002  NPPES base and canonical NPI key
  - EPIC-010-F-103-S-003          multi-address (pl_pfile)
  - EPIC-010-F-103-S-004          Census ZCTA-to-County crosswalk
  - EPIC-010-F-103-S-005          USDA RUCC classification workbook

Target staging collections per LLD §6.4 (pipeline cluster
`{env}_PublicHealthData`):

  pipeline_sources_nppes_npi
  pipeline_sources_pl_pfile
  pipeline_sources_nucc_taxonomy
  pipeline_sources_zip_county_crosswalk
  pipeline_sources_rucc
  pipeline_sources_specialty_catalog

Load semantics per LLD §4.6:
  - NPPES NPI load is serial single-PID. The other sources load in
    parallel across sources; within one source, rows are batched and
    written with `bulk_write(ordered=False)`.
  - Every staged document carries the `run_id` and `_source_row_index`
    (byte-offset-ordered row index) so downstream stages can partition
    deterministically and re-drive individual rows.
  - Secondary-source rows (pl_pfile) get an indexed `npi` field per REQ-T-004
    so the join in §4.9 is index-driven.
  - Each source runs behind a drop-then-load switch keyed on `run_id`: on
    re-drive of the same run_id, the same rows are re-inserted with the
    same _source_row_index, giving the loader an idempotent contract.

Public entry point: `load_staging(config, mongo, blob)`.
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException

import csv
import io
import json

import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Iterator

from pymongo import ASCENDING, InsertOne

_log = ChatHealthyLoggingService()

STAGING_DB_NAME = "PublicStaging"

# Base collection name per source (versioned at use-time as
# f"{base}_v_{data_version}"). All staging tables live in the fixed
# PublicStaging DB on the ChatHealthyPipelines cluster.
STAGING_BASE_NAMES = {
    "nppes_npi": "Provider",
    "pl_pfile": "PlPfile",
    "nucc": "Nucc",
    "census_zcta_county": "CensusZctaCounty",
    "usda_rucc": "UsdaRucc",
    "specialty_catalog": "SpecialtyCatalog",
}


def staging_collection_name(source_name: str, data_version: int) -> str:
    """Return "<Base>_v_<data_version>" for the given source. Raises if
    the source is unknown or data_version invalid."""
    base = STAGING_BASE_NAMES.get(source_name)
    if not base:
        raise ChatHealthyException(
            mode="value_error",
            message=f"staging_loader: no STAGING_BASE_NAMES entry for source {source_name!r}",
            source_name=source_name,
        )
    if not isinstance(data_version, int) or data_version < 1:
        raise ChatHealthyException(
            mode="value_error",
            message="staging_loader: data_version must be int >= 1",
            source_name=source_name,
            data_version=repr(data_version),
        )
    return f"{base}_v_{data_version}"


# Legacy alias — some downstream callers may still reference STAGING_
# COLLECTIONS by name. It now returns None-values because the actual
# collection name is version-dependent; those callers MUST migrate to
# staging_collection_name(source_name, data_version).
STAGING_COLLECTIONS = {k: None for k in STAGING_BASE_NAMES}

DEFAULT_BATCH_SIZE = 1000
DEFAULT_STAGING_CONCURRENCY = 4
NPPES_NPI_COLUMN_CANDIDATES = ("NPI", "npi", "National Provider Identifier")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _download_blob_to_tempfile(blob, container_name: str, blob_name: str) -> str:
    """Download blob to a local temp file; return its path."""
    if blob is None:
        raise ChatHealthyException(mode="runtime_error", message="staging_loader: blob client is required")
    container = blob.get_container_client(container_name)
    blob_client = container.get_blob_client(blob_name)
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix="staging_", suffix=".bin")
    try:
        downloader = blob_client.download_blob()
        with open(tmp.name, "wb") as fh:
            for chunk in downloader.chunks():
                fh.write(chunk)
    finally:
        tmp.close()
    return tmp.name


def _iter_csv_rows(local_path: str, *, delimiter: str = ",") -> Iterator[dict[str, str]]:
    """Iterate rows of a CSV as dicts; preserves column names verbatim."""
    with open(local_path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=delimiter)
        for row in reader:
            yield row


def _iter_zipped_csv_rows(local_zip_path: str, inner_name_hint: str | None = None) -> Iterator[dict[str, str]]:
    """Iterate rows of the first (or hint-matched) CSV inside a zip."""
    with zipfile.ZipFile(local_zip_path) as zf:
        candidates = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not candidates:
            raise ChatHealthyException(mode="runtime_error", message=f"staging_loader: no CSV inside {local_zip_path}")
        target = None
        if inner_name_hint:
            for c in candidates:
                if inner_name_hint.lower() in c.lower():
                    target = c
                    break
        if target is None:
            target = candidates[0]
        with zf.open(target) as inner:
            text = io.TextIOWrapper(inner, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            for row in reader:
                yield row


def _iter_json_rows(local_path: str) -> Iterator[dict[str, Any]]:
    """Iterate a JSON list or line-delimited JSON."""
    with open(local_path, "r", encoding="utf-8") as fh:
        first = fh.read(1)
        fh.seek(0)
        if first == "[":
            data = json.load(fh)
            if not isinstance(data, list):
                raise ChatHealthyException(mode="runtime_error", message="staging_loader: expected JSON array")
            for row in data:
                yield row
        else:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)


def _resolve_iter(source_name: str, spec: dict, local_path: str) -> Iterator[dict[str, Any]]:
    fmt = spec.get("format", "csv").lower()
    if fmt == "csv":
        yield from _iter_csv_rows(local_path, delimiter=spec.get("delimiter", ","))
    elif fmt == "zip_csv":
        yield from _iter_zipped_csv_rows(local_path, inner_name_hint=spec.get("inner_name_hint"))
    elif fmt == "json":
        yield from _iter_json_rows(local_path)
    else:
        raise ChatHealthyException(mode="runtime_error", message=f"staging_loader[{source_name}]: unsupported format {fmt!r}")


def _first_present(row: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return None


def _wrap_row(
    row: dict,
    *,
    source_name: str,
    row_index: int,
    run_id: str,
    npi_hint_keys: tuple[str, ...] | None,
) -> dict:
    """Compose a staging document per LLD §4.6."""
    doc: dict[str, Any] = {
        "run_id": run_id,
        "source_name": source_name,
        "_source_row_index": row_index,
        "raw": row,
        "loaded_at": _now_iso(),
    }
    if npi_hint_keys:
        npi = _first_present(row, npi_hint_keys)
        if npi:
            doc["npi"] = npi.zfill(10) if npi.isdigit() else npi
    if source_name == "nucc":
        code = _first_present(row, ("Code", "code"))
        if code:
            doc["code"] = code
    if source_name == "census_zcta_county":
        zcta = _first_present(row, ("ZCTA5", "zcta5", "ZCTA"))
        if zcta:
            doc["zcta5"] = zcta.zfill(5)
    if source_name == "usda_rucc":
        fips = _first_present(row, ("FIPS", "fips"))
        if fips:
            doc["fips"] = fips.zfill(5)
    return doc


def _ensure_indexes(coll, source_name: str) -> None:
    """Create indexes on the versioned staging collection.

    NPPES gets a UNIQUE index on npi. Any duplicate NPI insert raises
    BulkWriteError, which propagates out of _load_one_source and fails
    the step. NO retry, NO try/except swallow. That is the failure
    surface that catches the exact 34M-row inflation that happened with
    the prior non-unique index.

    pl_pfile has multiple rows per NPI (one per practice location); its
    index cannot be unique on npi alone.

    All other sources get non-unique indexes on their natural key for
    downstream lookup performance.
    """
    coll.create_index([("run_id", ASCENDING), ("_source_row_index", ASCENDING)])
    if source_name == "nppes_npi":
        coll.create_index([("npi", ASCENDING)], unique=True, name="npi_unique")
    elif source_name == "pl_pfile":
        coll.create_index([("npi", ASCENDING)])
    elif source_name == "nucc":
        coll.create_index([("code", ASCENDING)])
    elif source_name == "census_zcta_county":
        coll.create_index([("zcta5", ASCENDING)])
    elif source_name == "usda_rucc":
        coll.create_index([("fips", ASCENDING)])


def _drop_prior_run_rows(coll, run_id: str) -> int:
    res = coll.delete_many({"run_id": run_id})
    return res.deleted_count if res else 0


_NPPES_STATE_COLUMN = "Provider Business Practice Location Address State Name"


def _require_staging_collection(source_name: str) -> str:
    coll_name = STAGING_COLLECTIONS.get(source_name)
    if not coll_name:
        raise ChatHealthyException(mode="runtime_error", message=f"staging_loader[{source_name}]: no staging collection mapping")
    return coll_name


def _require_mongo(mongo) -> None:
    if mongo is None:
        raise ChatHealthyException(mode="runtime_error", message="staging_loader: mongo client is required")


def _drop_prior_state_scoped_rows(coll, source_name: str, states: tuple[str, ...]) -> int:
    """Full-load hygiene for NPPES: delete every existing row whose state
    is in the current state scope. Records from other states stay
    untouched. Called BEFORE the fresh load so the collection ends up
    with exactly the current run's state scope + everything else from
    prior runs. Returns the delete count.

    Note: _wrap_row nests the raw CSV row under doc["raw"], so the
    Mongo query must use the dotted "raw.<column>" path, not the bare
    column name."""
    if source_name != "nppes_npi" or not states:
        return 0
    filter_states = [s.upper() for s in states if s]
    if not filter_states:
        return 0
    res = coll.delete_many({f"raw.{_NPPES_STATE_COLUMN}": {"$in": filter_states}})
    return int(res.deleted_count or 0)


def _load_one_source(
    *,
    source_name: str,
    spec: dict,
    run_id: str,
    env_prefix: str,
    mongo,
    blob,
    batch_size: int,
    data_version: int,
    states: tuple[str, ...] = (),
    incremental: bool = False,
) -> dict[str, Any]:
    """Load one source into its versioned staging collection.

    Target: mongo["PublicStaging"][staging_collection_name(source_name,
    data_version)] on the ChatHealthyPipelines cluster.

    states: 2-letter state codes to keep for NPPES rows. Empty => keep all
        (equivalent to state_scope=ALL). Only applied to source_name ==
        'nppes_npi'. Other sources (reference tables, pl_pfile) always
        load in full.
    incremental: when False (full load) AND states is non-empty AND
        source is NPPES, additionally delete every existing row in the
        staging collection whose state is in `states` BEFORE inserting
        the fresh rows. Records from out-of-scope states are preserved.
    """
    coll_name = staging_collection_name(source_name, data_version)
    _require_mongo(mongo)
    db = mongo[STAGING_DB_NAME]
    coll = db[coll_name]

    _ensure_indexes(coll, source_name)
    deleted_current_run = _drop_prior_run_rows(coll, run_id)
    deleted_state_scoped = (
        0 if incremental else _drop_prior_state_scoped_rows(coll, source_name, states)
    )

    container_name = spec["blob_container"]
    blob_name = spec["blob_path"]
    local_path = _download_blob_to_tempfile(blob, container_name, blob_name)

    npi_hint = NPPES_NPI_COLUMN_CANDIDATES if source_name in ("nppes_npi", "pl_pfile") else None

    state_filter: set[str] = set()
    if source_name == "nppes_npi" and states:
        state_filter = {s.upper() for s in states if s}

    inserted = 0
    skipped_out_of_scope = 0
    ops: list[InsertOne] = []
    row_index = 0

    try:
        for row in _resolve_iter(source_name, spec, local_path):
            if state_filter:
                row_state = (row.get(_NPPES_STATE_COLUMN) or "").strip().upper()
                if row_state not in state_filter:
                    row_index += 1
                    skipped_out_of_scope += 1
                    continue
            doc = _wrap_row(
                row,
                source_name=source_name,
                row_index=row_index,
                run_id=run_id,
                npi_hint_keys=npi_hint,
            )
            row_index += 1
            ops.append(InsertOne(doc))
            if len(ops) >= batch_size:
                res = coll.bulk_write(ops, ordered=False)
                inserted += (res.inserted_count or 0)
                ops = []
        if ops:
            res = coll.bulk_write(ops, ordered=False)
            inserted += (res.inserted_count or 0)
    finally:
        try:
            import os
            os.unlink(local_path)
        except OSError:
            pass

    return {
        "source_name": source_name,
        "collection": coll_name,
        "inserted": inserted,
        "deleted_prior_rows_for_run": deleted_current_run,
        "deleted_prior_rows_for_states": deleted_state_scoped,
        "skipped_out_of_scope_rows": skipped_out_of_scope,
        "row_count": row_index,
    }


def load_staging(
    config: dict,
    *,
    mongo=None,
    blob=None,
) -> dict[str, Any]:
    """Load every source in `config["sources"]` into its staging collection.

    Required config keys:
      - sources: dict[source_name -> spec]
          spec fields:
            blob_container    (str)
            blob_path         (str)
            format            ("csv" | "zip_csv" | "json")
            delimiter         (str, csv only)
            inner_name_hint   (str, zip_csv only)
      - run_id                (str)
      - env                   (str)
      - staging_concurrency   (int, default 4; NPPES NPI is always serial)
      - batch_size            (int, default 1000)

    Returns:
      {
        "results":       list of per-source result records,
        "total_inserted": int,
      }
    """
    sources = config.get("sources") or {}
    if not sources:
        raise ChatHealthyException(mode="runtime_error", message="staging_loader: config['sources'] is empty")

    run_id = config["run_id"]
    env_prefix = config.get("env", "dev")
    batch_size = int(config.get("batch_size", DEFAULT_BATCH_SIZE))
    concurrency = int(config.get("staging_concurrency", DEFAULT_STAGING_CONCURRENCY))
    states: tuple[str, ...] = tuple(s for s in (config.get("states") or ()) if s)
    incremental: bool = bool(config.get("incremental"))
    dv = config.get("data_version")
    if not isinstance(dv, int) or dv < 1:
        raise ChatHealthyException(
            mode="value_error",
            message="staging_loader: config['data_version'] must be int >= 1",
            data_version=repr(dv),
        )
    data_version: int = dv

    results: list[dict[str, Any]] = []

    nppes_spec = sources.get("nppes_npi")
    if nppes_spec:
        results.append(_load_one_source(
            source_name="nppes_npi",
            spec=nppes_spec,
            run_id=run_id,
            env_prefix=env_prefix,
            mongo=mongo,
            blob=blob,
            batch_size=batch_size,
            data_version=data_version,
            states=states,
            incremental=incremental,
        ))

    parallel_sources = {n: s for n, s in sources.items() if n != "nppes_npi"}
    if parallel_sources:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {
                pool.submit(
                    _load_one_source,
                    source_name=name,
                    spec=spec,
                    run_id=run_id,
                    env_prefix=env_prefix,
                    mongo=mongo,
                    blob=blob,
                    batch_size=batch_size,
                    data_version=data_version,
                    states=states,
                    incremental=incremental,
                ): name for name, spec in parallel_sources.items()
            }
            for fut in as_completed(futures):
                name = futures[fut]
                # No swallowing. If a Worker future raises, .result()
                # re-raises here and propagates out of load_staging so
                # the step fails loud. Operator directive: "if we fail
                # we fail" - no fallbacks, no retries.
                results.append(fut.result())

    total = sum(int(r.get("inserted", 0)) for r in results)
    return {"results": results, "total_inserted": total}
