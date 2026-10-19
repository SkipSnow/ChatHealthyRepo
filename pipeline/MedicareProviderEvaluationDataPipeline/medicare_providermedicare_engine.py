# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ProviderMedicare build engine (EPIC-010-F-007, III.2) — provider-core model.

Builds providerMedicarePartDQuantityRollUps: ONE document per provider x
molecule (the flat grain). The provider is the core object (EMR-style, rooted on
the provider rather than the patient); each document is one molecule that
provider prescribes, enriched for the evaluation query. _id is "<npi>:<molecule_id>".

The build fans out by prescriber state: every row of a provider carries one
Prscrbr_State, so a state worker sees each of its providers' rows in full and
groups them by (npi, molecule_id) with no cross-worker contention. Usage sums
over the molecule's brand rows; beneficiaries are non-additive (see
aggregate_provider_molecule). NUCC and entity type come from Provider_v_5 by NPI;
CMS Prscrbr_Type is dropped. The indications/drug_class/identity are left empty
here and attached by the enrich pass from the NormalizedIndicationMap / FDA file
by molecule_id. The (indications, npi) search index is created in provisioning.

Conformance: Sum over all documents of record_count == CMS Part D row count.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS,
    aggregate_provider_molecule,
    molecule_id,
)

_log = ChatHealthyLoggingService()


def _cms_state_filter(state: str, state_col: str) -> dict:
    """Select one fan-out partition's staged Part D rows by the CMS prescriber
    state column. "ALL" is the catch-all worker for rows whose state is none of
    the fifty-plus-DC codes (territories, military, foreign, blank)."""
    field = f"raw.{state_col}"
    if state == "ALL":
        # Lazy import: importing run_lifecycle.steps at module load created a
        # circular import that stopped build_providermedicare_base from registering.
        from pipeline.run_lifecycle.steps._partitions import ALL_US_STATES
        return {field: {"$nin": list(ALL_US_STATES)}}
    return {field: state}


def _raw_rows(coll, run_id: str, state: str, state_col: str) -> list[dict]:
    return [doc.get("raw") or {}
            for doc in coll.find({"run_id": run_id, **_cms_state_filter(state, state_col)})]


def _nucc_codes(spine_doc: dict) -> list[str]:
    codes: list[str] = []
    for tax in spine_doc.get("taxonomies") or []:
        code = (tax or {}).get("code") if isinstance(tax, dict) else None
        if code and code not in codes:
            codes.append(code)
    return codes


def _entity_type(spine_doc: dict) -> str:
    return "O" if str(spine_doc.get("entity_type_code") or "").strip() == "2" else "I"


def _spine_by_npi(provider_spine_coll, npis: set[str], chunk: int = 1000) -> dict[str, dict]:
    """Resolve the Provider_v_5 spine doc for each NPI (chunked $in on the
    NPI-keyed Provider collection). Returns npi -> spine doc."""
    out: dict[str, dict] = {}
    batch: list[str] = []

    def _flush() -> None:
        if not batch:
            return
        for spine in provider_spine_coll.find({"npi": {"$in": batch}}):
            key = str(spine.get("npi") or spine.get("_id") or "").strip()
            if key:
                out[key] = spine
        batch.clear()

    for npi in npis:
        batch.append(npi)
        if len(batch) >= chunk:
            _flush()
    _flush()
    return out


def build_provider_molecule(
    *,
    state: str,
    run_id: str,
    data_version: int,
    provider_spine_coll,
    partd_coll,
    target_coll,
    entity_type: int | None = None,
    batch_size: int = 1000,
) -> dict[str, Any]:
    """Build the provider x molecule documents for one fan-out state. Groups the
    state's Part D rows by (npi, molecule_id), aggregates each group, and upserts
    one document per pair. Returns the write count, the de-duped prescribing NPIs
    absent from Provider_v_5, and the count of rows with no Gnrc_Name (which
    cannot be placed and will surface in the conformance delta)."""
    partd_rows = _raw_rows(partd_coll, run_id, state, PARTD_COLS["state"])

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    npis: set[str] = set()
    no_generic = 0
    for raw in partd_rows:
        npi = str(raw.get(PARTD_COLS["npi"]) or "").strip()
        mid = molecule_id(raw.get(PARTD_COLS["generic"]))
        if not npi or not mid:
            no_generic += 1
            continue
        npis.add(npi)
        groups[(npi, mid)].append(raw)

    spine_by_npi = _spine_by_npi(provider_spine_coll, npis)
    not_found = sorted(npis - set(spine_by_npi))
    provenance = {"source_run_id": run_id,
                  "source_collection": "StagingMedicarePartD_v_1",
                  "built_at": datetime.now(timezone.utc).isoformat()}

    ops: list[UpdateOne] = []
    written = 0
    for (npi, mid), rows in groups.items():
        spine = spine_by_npi.get(npi) or {}
        if entity_type is not None:
            # Type 1 (individual) / Type 2 (institutional) are disjoint and
            # exhaustive, so the (state,1) and (state,2) partitions together
            # cover every row -- conformance is preserved. An NPI absent from
            # the spine has no entity and falls to the individual partition.
            npi_entity = 2 if str(spine.get("entity_type_code") or "").strip() == "2" else 1
            if npi_entity != entity_type:
                continue
        block = aggregate_provider_molecule(rows)
        doc = {
            "_id": f"{npi}:{mid}",
            "npi": npi,
            "prescriber_state": block["state"] or state,
            "nucc_codes": _nucc_codes(spine),
            "entity_type": _entity_type(spine),
            "molecule_id": mid,
            "gnrc_name": block["gnrc_name"],
            "brands": block["brands"],
            "record_count": block["record_count"],
            "totals": block["totals"],
            "ge65": block["ge65"],
            "benes": block["benes"],
            # Filled by the enrich pass from the NormalizedIndicationMap / FDA
            # file by molecule_id; indications is the (indications, npi) search key.
            "indications": [],
            "drug_class": None,
            "identity": {},
            "provenance": provenance,
        }
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True))
        if len(ops) >= batch_size:
            target_coll.bulk_write(ops, ordered=False)
            written += len(ops)
            ops = []
    if ops:
        target_coll.bulk_write(ops, ordered=False)
        written += len(ops)

    # The (indications, npi, molecule_id, quantities) search index is provisioned
    # declaratively by the medicare_providermedicare_indexes package in
    # deployment_architecture.json (indexes live in provisioning, not in-run).
    _log.LogPipeline(
        "INFO",
        "build_provider_molecule[%s]: rows=%d pairs=%d written=%d npis=%d not_found=%d no_generic=%d",
        state, len(partd_rows), len(groups), written, len(npis), len(not_found), no_generic,
    )
    return {
        "state": state,
        "records_written": written,
        "pairs": len(groups),
        "not_found_npis": not_found,
        "no_generic_rows": no_generic,
    }
