# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ProviderMedicare base build engine (EPIC-010-F-007).

The CMS Part B / Part D files are denormalized and joint-keyed (many rows per
NPI), national. This build NORMALIZES and AGGREGATES those rows up to our grain
(one ProviderMedicare document per NPI): procedures collapsed to one per
HCPCS_Cd, drugs carried raw per Part D row (the RXCUI re-grain lives in the
enrich pass). The normalization/aggregation itself is the pure functions in
medicare_normalization; this engine fetches staging rows, drives those functions,
joins to the Provider spine BY NPI, and writes the documents.

The join is by NPI directly against the NPI-keyed Provider collection -- never
gated by matching the CMS state column to the provider's business-mailing state.
State is only the fan-out/parallelism dimension: each worker processes the
Part B / Part D rows for one CMS state value and resolves each NPI it finds
against the whole Provider collection. A billed/prescribing NPI absent from the
Provider collection is returned as a not-found NPI (de-duped to the distinct
set) for the step to record as one finding per distinct NPI.
"""

from __future__ import annotations

from typing import Any

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

from pipeline.run_lifecycle.steps._partitions import ALL_US_STATES
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTB_COLS,
    PARTD_COLS,
    aggregate_procedures,
    collect_drugs,
    distinct_billed_npis,
    npis_not_found,
)

_log = ChatHealthyLoggingService()


def _cms_state_filter(state: str, state_col: str) -> dict:
    """Select one fan-out partition's staged CMS rows by their own state column.

    The twin of business_state_filter, but on the CMS source state column
    (raw.<state_col>) rather than the provider's business-mailing state. "ALL"
    is the catch-all worker -- every row whose CMS state is none of the fifty-
    plus-DC codes (territories, military, foreign, blank). This partitions the
    national Part B / Part D rows across the workers; it is NOT the join key.
    """
    field = f"raw.{state_col}"
    if state == "ALL":
        return {field: {"$nin": list(ALL_US_STATES)}}
    return {field: state}


def _raw_rows(coll, run_id: str, state: str, state_col: str) -> list[dict]:
    return [doc.get("raw") or {}
            for doc in coll.find({"run_id": run_id, **_cms_state_filter(state, state_col)})]


def _entity_type(spine_doc: dict) -> str:
    code = str(spine_doc.get("entity_type_code") or "").strip()
    return "O" if code == "2" else "I"


def _specialty_codes(spine_doc: dict) -> list[str]:
    codes: list[str] = []
    for tax in spine_doc.get("taxonomies") or []:
        code = (tax or {}).get("code") if isinstance(tax, dict) else None
        if code and code not in codes:
            codes.append(code)
    return codes


def _lookup_spines(provider_spine_coll, npis: set[str], chunk: int = 1000) -> dict[str, dict]:
    """Resolve the Provider spine for a set of NPIs, by NPI directly (chunked
    $in on the NPI-keyed Provider collection). Returns npi -> spine doc."""
    found: dict[str, dict] = {}
    batch: list[str] = []

    def _flush() -> None:
        if not batch:
            return
        for spine in provider_spine_coll.find({"npi": {"$in": batch}}):
            key = str(spine.get("npi") or spine.get("_id") or "").strip()
            if key:
                found[key] = spine
        batch.clear()

    for npi in npis:
        batch.append(npi)
        if len(batch) >= chunk:
            _flush()
    _flush()
    return found


def build_providermedicare(
    *,
    state: str,
    run_id: str,
    data_version: int,
    provider_spine_coll,
    partb_coll,
    partd_coll,
    target_coll,
    batch_size: int = 1000,
) -> dict[str, Any]:
    """Build ProviderMedicare docs for one fan-out state into target_coll.

    Returns the write count and the de-duped list of billed/prescribing NPIs
    absent from the Provider collection (not_found_npis).
    """
    partb_rows = _raw_rows(partb_coll, run_id, state, PARTB_COLS["state"])
    partd_rows = _raw_rows(partd_coll, run_id, state, PARTD_COLS["state"])

    procedures_by_npi = aggregate_procedures(partb_rows)
    drugs_by_npi = collect_drugs(partd_rows)
    billed_npis = distinct_billed_npis(partb_rows, partd_rows)

    # Join BY NPI against the whole Provider collection -- not within a state
    # bucket. An NPI present in Part B/D but absent from Provider is not-found.
    spine_by_npi = _lookup_spines(provider_spine_coll, billed_npis)
    not_found = npis_not_found(billed_npis, spine_by_npi.keys())

    ops: list[UpdateOne] = []
    written = 0
    for npi, spine in spine_by_npi.items():
        doc: dict[str, Any] = {
            "_id": npi,
            "npi": npi,
            "provider_state": state,
            "entity_type": _entity_type(spine),
            "specialty": _specialty_codes(spine),
            "run_id": run_id,
            "data_version": data_version,
            "procedures": procedures_by_npi.get(npi, []),
            "drugs": drugs_by_npi.get(npi, []),
            "indications": [],
        }
        # ccn / size_tier are written by enrich_org_ccn_size when known; the
        # schema forbids null for them, so the base record omits them.
        ops.append(UpdateOne({"_id": npi}, {"$set": doc}, upsert=True))
        if len(ops) >= batch_size:
            target_coll.bulk_write(ops, ordered=False)
            written += len(ops)
            ops = []
    if ops:
        target_coll.bulk_write(ops, ordered=False)
        written += len(ops)

    _log.LogPipeline(
        "INFO",
        "build_providermedicare[%s]: wrote=%d billed_npis=%d not_found=%d",
        state, written, len(billed_npis), len(not_found),
    )
    return {
        "state": state,
        "providermedicare_written": written,
        "billed_npis": len(billed_npis),
        "not_found_npis": not_found,
    }
