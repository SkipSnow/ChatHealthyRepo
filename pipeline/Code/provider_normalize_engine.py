# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Normalize staging NPPES rows into providers_v<N> — LLD §4.8."""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


from typing import Any

from pymongo import ReplaceOne

from pipeline_runtime import PipelineRuntime
from provider_record_builder import build_provider_record
from schemas.provider_record_validator import validate_provider_record

_log = ChatHealthyLoggingService()


def _nucc_lookup(rt: PipelineRuntime) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rt.staging_coll("nucc").find({"run_id": rt.run_id}):
        raw = row.get("raw") or {}
        code = raw.get("Code") or row.get("Code") or row.get("_id")
        if code:
            out[str(code)] = row
    return out


def serial_bulk_load(ctx) -> dict[str, Any]:
    rt = PipelineRuntime(ctx)
    nucc = _nucc_lookup(rt)
    # Full-mode drain: DELETE rows whose primary practice-address state
    # is in this run's state_scope. Two bugs fixed at once vs. the prior
    # coll.drop():
    #   (a) drop() nuked every index (npi_1, addresses.county.source_1,
    #       etc.) that prepare_infrastructure created — later steps
    #       (add_secondary_practices, county_enrichment, add-on lookups)
    #       then COLLSCAN every find({npi: ...}) and stall the run.
    #   (b) drop() ignored state_scope entirely and threw away every
    #       OTHER state's rows too — a run scoped to VT+DE was silently
    #       destroying CA, NY, and every other state's providers.
    # delete_many with the state filter leaves the collection object,
    # its indexes, and any schema validator intact; only in-scope rows
    # are removed and repopulated by the ReplaceOne(upsert=True) below.
    if not ctx.args.incremental:
        states = [s for s in (ctx.args.resolved_states() or []) if s]
        if states:
            state_filter = {
                "addresses": {"$elemMatch": {
                    "address_type": "practice",
                    "state": {"$in": [s.upper() for s in states]},
                }}
            }
            drained = rt.providers_coll.delete_many(state_filter).deleted_count
            _log.info(
                "serial_bulk_load: drained %d rows for state_scope=%s (indexes preserved)",
                drained, states,
            )

    seen_npis: set[str] = set()
    inserted = 0
    skipped_dup = 0
    violations = 0
    ops: list[ReplaceOne] = []
    batch_size = int(ctx.config.get("batch_limits", {}).get("normalize_batch_size", 1000))

    for row in rt.staging_coll("nppes_npi").find({"run_id": rt.run_id}):
        raw = row.get("raw") or {}
        npi = str(row.get("npi") or raw.get("NPI") or "").strip()
        if not npi:
            continue
        npi = npi.zfill(10)
        if npi in seen_npis:
            skipped_dup += 1
            continue
        seen_npis.add(npi)

        doc = build_provider_record(raw, npi=npi, run_id=rt.run_id, nucc_catalog=nucc)
        ok, errors = validate_provider_record(doc)
        if not ok:
            violations += 1
            rt.record_discrepancy(
                npi=npi,
                reason="schema_violation",
                step="normalize_npi_serial_bulk_load",
                state=rt.mailing_state(doc),
                entity_kind=rt.entity_kind(doc),
                detail={"errors": errors},
            )
            continue

        ops.append(ReplaceOne({"npi": npi}, doc, upsert=True))
        if len(ops) >= batch_size:
            result = rt.providers_coll.bulk_write(ops, ordered=False)
            inserted += result.upserted_count + result.modified_count
            ops = []

    if ops:
        result = rt.providers_coll.bulk_write(ops, ordered=False)
        inserted += result.upserted_count + result.modified_count

    return {
        "inserted": inserted,
        "unique_npis": len(seen_npis),
        "skipped_duplicate_staging_rows": skipped_dup,
        "schema_violations": violations,
        "target": rt.provider_collection,
    }


def per_state_fanout(ctx, state: str) -> dict[str, Any]:
    rt = PipelineRuntime(ctx)
    filt = rt.partition_filter(state)
    validated = 0
    for doc in rt.providers_coll.find(filt):
        ok, errors = validate_provider_record(doc)
        if ok:
            validated += 1
        else:
            rt.record_discrepancy(
                npi=doc.get("npi"),
                reason="schema_violation",
                step="normalize_npi_per_state_fanout",
                state=state,
                entity_kind=rt.entity_kind(doc),
                detail={"errors": errors},
            )
    return {"state": state, "validated": validated}
