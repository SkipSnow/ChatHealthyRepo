# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""SpecialtyMedicare rollup engine (EPIC-010-F-007, LLD v83 III.10/III.4.3).

Staging-sourced and serial: state-aggregated masters carry no per-NPI detail,
so the NUCC rollup streams StagingMedicarePartD directly, accumulates
per-(provider, molecule) all-ages totals, resolves NPI -> NUCC via chunked
Provider_v_5 reads, and writes one document per NUCC code as a deterministic
full-document upsert (single-writer; the collection was cleared by the
pre-pass, decision #1). Per (NUCC, molecule): specialty_total vs the
de-duplicated molecule_real_total, prescriber_count, and the operator-ruled
statistics (R1 2026-10-20) -- max / min (zeros excluded) / median (the middle
value) / avg (sum over count) of every quantitative measure over the
per-provider totals, computed here as the rollup's final pass.
provider_count comes from Provider_v_5 (taxonomies.code, indexed).
"""

from __future__ import annotations

import datetime
from typing import Any

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS,
    _bson,
    accumulate_provider_molecule_totals,
    rollup_specialty_v83,
)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_store_contract import (
    record_refused_writes)

_log = ChatHealthyLoggingService()


def _nucc_map(provider_coll, npis: list[str], chunk: int = 5000) -> dict[str, list[str]]:
    """npi -> distinct NUCC codes from the Provider collection's taxonomies."""
    out: dict[str, list[str]] = {}
    for i in range(0, len(npis), chunk):
        for doc in provider_coll.find(
                {"npi": {"$in": npis[i:i + chunk]}},
                {"npi": 1, "taxonomies.code": 1}):
            npi = str(doc.get("npi") or "").strip()
            codes: list[str] = []
            for tax in doc.get("taxonomies") or []:
                code = (tax or {}).get("code") if isinstance(tax, dict) else None
                if code and code not in codes:
                    codes.append(code)
            if npi:
                out[npi] = codes
    return out


def _provider_counts_by_nucc(provider_coll) -> dict[str, int]:
    """Every NUCC code's provider count, in one pass.

    The previous shape asked count_documents({"taxonomies.code": nucc}) once
    per NUCC code. Provider_v_5 carries no index on taxonomies.code, so each
    of those 717 counts was a scan of 9.67M documents. One unwind-and-group
    reads the collection once and answers every code.
    """
    out: dict[str, int] = {}
    for row in provider_coll.aggregate(
            [{"$unwind": "$taxonomies"},
             {"$group": {"_id": "$taxonomies.code",
                         "npis": {"$addToSet": "$npi"}}},
             {"$project": {"n": {"$size": "$npis"}}}],
            allowDiskUse=True):
        code = row.get("_id")
        if code:
            out[str(code)] = int(row.get("n") or 0)
    return out


def _in_scope_npis(settled_coll) -> set[str]:
    """The NPIs the settled ProviderMedicare collection still holds.

    drop_transient_attributes_and_records runs before this step and removes
    every record whose provider does not practise in an argued state. The
    criterion is a property of the provider, not of the molecule, so what
    survives is exactly the set of in-scope NPIs -- which is what makes this
    rollup a rollup of the settled collection rather than of everything CMS
    filed (operator ruling 2026-10-24).
    """
    # distinct on npi, not a $group over the whole collection: the published
    # collection carries an npi index (created by the clear-and-index step),
    # so this is an index scan rather than a read of 26.4M documents.
    return {str(npi) for npi in settled_coll.distinct("npi") if npi}


def _staging_pages(partd_coll, projection, molecule_ids: list[str] | None = None,
                   page_size: int = 5000):
    """Yield staged rows page by page, holding no cursor: a cursor idle past
    the server's reaper comes back CursorNotFound.

    molecule_ids is this worker's slice of the molecule list. The rows are
    found on the molecule_id__id index the load step creates, so a worker
    reads only the rows of the molecules it owns.
    """
    last_id = None
    while True:
        query: dict[str, Any] = {}
        if molecule_ids is not None:
            query["molecule_id"] = {"$in": molecule_ids}
        if last_id is not None:
            query["_id"] = {"$gt": last_id}
        page = list(partd_coll.find(query, {**projection, "_id": 1},
                                    sort=[("_id", 1)], limit=page_size))
        if not page:
            return
        last_id = page[-1]["_id"]
        yield from page


def _replace_conforming(target_coll, ops: list, keys: list[str], *,
                        record_discrepancy) -> int:
    """Write a batch of specialty cells; return how many the store took.

    UNORDERED, so a record the collection's validator refuses leaves the
    conforming records in the same batch written. Each refused record becomes
    its own per-record finding carrying its NUCC code.
    """
    try:
        target_coll.bulk_write(ops, ordered=False)
        return len(ops)
    except BulkWriteError as exc:
        refused = record_refused_writes(
            details=exc.details,
            keys=keys,
            artifact="specialtymedicare",
            step="rollup_specialtymedicare",
            record_discrepancy=record_discrepancy,
            exception=exc,
        )
        return len(ops) - refused


def rollup_specialtymedicare(
    *,
    run_id: str,
    partd_coll,
    provider_coll,
    target_coll,
    settled_coll,
    record_discrepancy,
    molecule_ids: list[str] | None = None,
    batch_size: int = 200,
) -> dict[str, Any]:
    """Build specialtyMedicarePartDQuantityRollUps for the run (serial).

    The measures come from the staged Part D rows, which carry the 30-day
    fills and the drug cost the R1 statistics need and the provider record
    does not. The SCOPE comes from the settled ProviderMedicare collection: a
    row whose NPI no longer appears there was dropped as out of scope and must
    not reach the specialty profile.
    """
    projection = {f"raw.{PARTD_COLS[k]}": 1 for k in
                  ("npi", "generic", "total_claims", "total_30day_fills",
                   "days_supply", "total_drug_cost")}
    projection["molecule_id"] = 1
    in_scope = _in_scope_npis(settled_coll)
    npi_field = PARTD_COLS["npi"]

    def _scoped_rows():
        for doc in _staging_pages(partd_coll, projection, molecule_ids):
            raw = doc.get("raw") or {}
            if str(raw.get(npi_field) or "").strip() in in_scope:
                yield raw

    # No run_id filter: the drained-and-replaced staging is the one truth
    # (Start At, operator design 2026-10-22). run_id stays on provenance only.
    acc = accumulate_provider_molecule_totals(_scoped_rows())

    npis = sorted({npi for npi, _ in acc})
    nucc_by_npi = _nucc_map(provider_coll, npis)
    by_nucc = rollup_specialty_v83(acc, nucc_by_npi)

    built_at = datetime.datetime.utcnow().isoformat()
    ops: list[UpdateOne] = []
    keys: list[str] = []
    written = 0
    offered = 0
    provider_counts = _provider_counts_by_nucc(provider_coll)
    for nucc in sorted(by_nucc):
        # Every value in a specialty record is per (NUCC, molecule), so a
        # worker's cells are disjoint from every other worker's and each is
        # written whole with $set. The record's own identity and provenance
        # are the same for every worker, so they are set alongside.
        setter: dict[str, Any] = {
            "nucc_code": nucc,
            "provider_count": provider_counts.get(nucc, 0),
            "provenance": {
                "aggregated_from": ["providerMedicarePartDQuantityRollUps",
                                    "StagingMedicarePartD_v_1", "Provider_v_5"],
                "run_id": run_id,
                "built_at": built_at,
            },
        }
        for mid in sorted(by_nucc[nucc]):
            setter[f"molecules.{mid}"] = by_nucc[nucc][mid]
        # Decimal is not BSON-encodable: every exact value becomes Decimal128
        # here, at the one write boundary, so arithmetic above stays in
        # Decimal and nothing is rounded on the way to the store.
        ops.append(UpdateOne({"_id": nucc}, {"$set": _bson(setter)},
                             upsert=True))
        keys.append(str(nucc))
        if len(ops) >= batch_size:
            offered += len(ops)
            written += _replace_conforming(
                target_coll, ops, keys, record_discrepancy=record_discrepancy)
            ops = []
            keys = []
    if ops:
        offered += len(ops)
        written += _replace_conforming(
            target_coll, ops, keys, record_discrepancy=record_discrepancy)

    _log.LogPipeline(
        "INFO",
        "rollup_specialtymedicare: pairs=%d npis=%d nuccs=%d written=%d "
        "refused=%d run_id=%s",
        len(acc), len(npis), len(by_nucc), written, offered - written, run_id)
    return {"provider_molecule_pairs": len(acc), "distinct_npis": len(npis),
            "nucc_docs_written": written,
            "nucc_docs_refused_by_contract": offered - written}
