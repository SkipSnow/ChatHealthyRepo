# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Normalize staging NPPES rows into providers_v<N> — LLD §4.8."""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService


from typing import Any

import pymongo
from chathealthy_lib.exceptions import ChatHealthyException
from pymongo import ASCENDING, InsertOne, ReplaceOne

from pipeline_runtime import PipelineRuntime
from provider_record_builder import build_provider_record
from schemas.provider_record_validator import validate_provider_record

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
    bulk_write. Replaces the prior two-step design (serial_bulk_load +
    per_state_fanout) which serialized on one worker and then re-validated
    what it just wrote. This runs 51-way in parallel with state_scope=ALL.

    Partition key: BUSINESS mailing address state (single-valued per NPI
    per NPPES contract). Practice addresses are optional and multi-valued
    (secondary practices); business mailing is required and unique, so it
    is the reliable NPI-atomic partition key.
    """
    rt = PipelineRuntime(ctx)
    state = (state or "").upper()
    if not state:
        raise ChatHealthyException(mode="value_error", message="per_state_normalize: state is required")
    nucc = _nucc_lookup(rt)

    # The collection this partition writes must carry its own indexes before
    # a single row lands. Creating them here, not in prepare_infrastructure,
    # because this is where the collection's name is actually resolved --
    # prepare ensured npi_1 on the name in pipeline.config and the writes went
    # somewhere else, so 45,000 rows were written to a collection whose only
    # index was _id_. create_index is idempotent.
    rt.providers_coll.create_index([("npi", ASCENDING)], name="npi_1")
    rt.providers_coll.create_index(
        [("addresses.address_type", ASCENDING), ("addresses.state", ASCENDING)],
        name="business_state_scan",
    )

    # Full-mode drain: DELETE rows whose BUSINESS mailing address state
    # matches this partition. Preserves indexes (delete_many, not drop()).
    drained = 0
    if not ctx.args.incremental:
        drained = rt.providers_coll.delete_many({
            "addresses": {"$elemMatch": {"address_type": "business", "state": state}},
        }).deleted_count

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
    query = {"run_id": rt.run_id, f"raw.{_NPPES_STATE_COLUMN}": state}
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

    return {
        "state": state,
        "drained": drained,
        "inserted": inserted,
        "unique_npis": len(seen_npis),
        "skipped_duplicate_staging_rows": skipped_dup,
        "schema_violations": violations,
    }
