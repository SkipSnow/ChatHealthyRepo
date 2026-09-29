# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Normalize staging NPPES rows into providers_v<N> — LLD §4.8."""

from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService


from typing import Any

import pymongo
from chathealthy_lib.exceptions import ChatHealthyException
from pymongo import InsertOne, ReplaceOne

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.provider_base.provider_record_builder import build_provider_record
from pipeline.provider_base.provider_record_validator import validate_provider_record

_log = ChatHealthyLoggingService()


def _nucc_lookup(rt: PipelineRuntime) -> dict[str, dict]:
    """Return code -> SMD row for every published specialty.

    v42 §5.2.9 Pass B: reads the pipeline cluster's fully-published SMD
    collection, resolved from the registry's `smd` entry rather than
    named here, which the ordered step list guarantees is complete
    before this function is called (publish_smd_and_embed is a
    transitive prerequisite of normalize_npi_per_state_fanout).
    Includes both native NUCC codes and F-105 supplements (e.g.
    246ZS0400X).

    Pipeline-cluster read only. Pipelines never touch the front-end
    cluster (operator directive 2026-08-02).

    The SMD row has the display fields at the top level (Code, Display
    Name, Grouping, Classification, Specialization, Definition). To keep
    the caller (build_provider_record) untouched, wrap the flat SMD row
    in the {'raw': {...}} shape build_provider_record expects.
    """
    smd_entry = rt.registry.by_source_name("smd")
    smd = rt.mongo[smd_entry.public_data_db][
        rt.registry.public_data_collection_name("smd")
    ]
    out: dict[str, dict] = {}
    for row in smd.find({}):
        code = row.get("Code") or row.get("code")
        if not code:
            continue
        out[str(code)] = {"raw": row}
    return out


_NPPES_STATE_COLUMN = "Provider Business Mailing Address State Name"


def per_state_normalize(ctx, state: str) -> dict[str, Any]:
    """Per-state normalize (NPI-atomic ownership): drain this state's rows
    in the target, read only this state's staging rows, build + validate +
    bulk_write. Runs 52-way in parallel under state_scope=ALL (one worker per
    US state plus the "ALL" catch-all).

    Partition key: BUSINESS mailing address state (single-valued per NPI
    per NPPES contract). Practice addresses are optional and multi-valued
    (secondary practices); business mailing is required and unique, so it
    is the reliable NPI-atomic partition key.
    """
    from pipeline.run_lifecycle.steps._partitions import (  # noqa: PLC0415
        ALL_US_STATES, business_state_filter)
    rt = PipelineRuntime(ctx)
    state = (state or "").upper()
    if not state:
        raise ChatHealthyException(mode="value_error", message="per_state_normalize: state is required")
    nucc = _nucc_lookup(rt)

    # Ensure this partition's target indexes before any row lands, here where
    # the collection name is actually resolved. apply_indexes is idempotent.
    from pipeline.run_lifecycle.ensure_provider_indexes_activity import _pipeline_provider_index_specs  # noqa: PLC0415
    from chathealthy_lib.mongo_indexes import apply_indexes  # noqa: PLC0415
    apply_indexes(rt.providers_coll, _pipeline_provider_index_specs())

    # Full-mode drain: DELETE rows whose BUSINESS mailing address state
    # matches this partition. Preserves indexes (delete_many, not drop()).
    drained = 0
    if not ctx.args.incremental:
        drained = rt.providers_coll.delete_many(
            business_state_filter(state)).deleted_count

    seen_npis: set[str] = set()
    inserted = 0
    skipped_dup = 0
    violations = 0
    ops: list = []
    batch_size = int(ctx.config.get("batch_limits", {}).get("normalize_batch_size", 1000))

    # Read only this state's staging rows. staging_load filters by
    # state_scope at ingest, so with state_scope=ALL every state's rows
    # are present here; per-state fanout partitions them for parallel
    # normalize. Wrap in pymongo.timeout(3600) to override the client-
    # level timeoutMS (120s) — big states take longer than 2 min to
    # iterate + build + validate + write. no_cursor_timeout=True also
    # prevents server-side cursor idle kill.
    # The drain (business_address.state) and this read (the raw NPPES state
    # column) must select the same providers. "ALL" is the catch-all worker:
    # both sides read it as "business state is none of ALL_US_STATES" -- the
    # drain via business_state_filter, this read via the same $nin below.
    if state == "ALL":
        staging_state: dict = {"$nin": ALL_US_STATES}
    else:
        staging_state = state
    query = {"run_id": rt.run_id, f"raw.{_NPPES_STATE_COLUMN}": staging_state}
    with pymongo.timeout(3600):
        for row in rt.staging_coll("nppes_npi").find(query, no_cursor_timeout=True):
            raw = row.get("raw") or {}
            npi = str(row.get("npi") or raw.get("NPI") or "").strip()
            if not npi:
                continue
            npi = npi.zfill(10)
            if npi in seen_npis:
                skipped_dup += 1
                continue
            seen_npis.add(npi)

            doc = build_provider_record(
                raw, npi=npi, run_id=rt.run_id, nucc_catalog=nucc,
            )
            ok, errors = validate_provider_record(doc)
            if not ok:
                violations += 1
                rt.record_discrepancy(
                    npi=npi,
                    reason="schema_violation",
                    step="normalize_npi_per_state_fanout",
                    state=state,
                    entity_kind=rt.entity_kind(doc),
                    detail={"errors": errors},
                )
                continue

            # A full run has just drained this state, so no row can match:
            # ReplaceOne would pay for a lookup that cannot succeed, once per
            # record. Insert what we know is new; replace only when there may
            # genuinely be something to replace.
            ops.append(InsertOne(doc) if not ctx.args.incremental
                       else ReplaceOne({"npi": npi}, doc, upsert=True))
            if len(ops) >= batch_size:
                result = rt.providers_coll.bulk_write(ops, ordered=False)
                inserted += (result.inserted_count + result.upserted_count
                             + result.modified_count)
                ops = []

        if ops:
            result = rt.providers_coll.bulk_write(ops, ordered=False)
            inserted += (result.inserted_count + result.upserted_count
                         + result.modified_count)

    _log.LogPipeline(
        "INFO",
        "per_state_normalize state=%s drained=%d inserted=%d unique_npis=%d "
        "skipped_dup=%d schema_violations=%d",
        state, drained, inserted, len(seen_npis), skipped_dup, violations,
    )
    return {
        "state": state,
        "drained": drained,
        "inserted": inserted,
        "unique_npis": len(seen_npis),
        "skipped_duplicate_staging_rows": skipped_dup,
        "schema_violations": violations,
    }
