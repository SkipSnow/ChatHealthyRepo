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
from pymongo import ReplaceOne

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS,
    accumulate_provider_molecule_totals,
    rollup_specialty_v83,
)

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


def _in_scope_npis(settled_coll) -> set[str]:
    """The NPIs the settled ProviderMedicare collection still holds.

    drop_transient_attributes_and_records runs before this step and removes
    every record whose provider does not practise in an argued state. The
    criterion is a property of the provider, not of the molecule, so what
    survives is exactly the set of in-scope NPIs -- which is what makes this
    rollup a rollup of the settled collection rather than of everything CMS
    filed (operator ruling 2026-10-24).
    """
    return {str(d["_id"]) for d in settled_coll.aggregate(
        [{"$group": {"_id": "$npi"}}], allowDiskUse=True) if d.get("_id")}


def _staging_pages(partd_coll, projection, page_size: int = 5000):
    """Yield staged rows page by page, holding no cursor: a cursor idle past
    the server's reaper comes back CursorNotFound."""
    last_id = None
    while True:
        query: dict[str, Any] = {}
        if last_id is not None:
            query["_id"] = {"$gt": last_id}
        page = list(partd_coll.find(query, {**projection, "_id": 1},
                                    sort=[("_id", 1)], limit=page_size))
        if not page:
            return
        last_id = page[-1]["_id"]
        yield from page


def rollup_specialtymedicare(
    *,
    run_id: str,
    partd_coll,
    provider_coll,
    target_coll,
    settled_coll,
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
    in_scope = _in_scope_npis(settled_coll)
    npi_field = PARTD_COLS["npi"]

    def _scoped_rows():
        for doc in _staging_pages(partd_coll, projection):
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
    ops: list[ReplaceOne] = []
    written = 0
    for nucc in sorted(by_nucc):
        molecules = [by_nucc[nucc][mid] for mid in sorted(by_nucc[nucc])]
        doc = {
            "_id": nucc,
            "nucc_code": nucc,
            "molecules": molecules,
            "provider_count": provider_coll.count_documents(
                {"taxonomies.code": nucc}),
            "provenance": {
                "aggregated_from": ["providerMedicarePartDQuantityRollUps",
                                    "StagingMedicarePartD_v_1", "Provider_v_5"],
                "run_id": run_id,
                "built_at": built_at,
            },
        }
        ops.append(ReplaceOne({"_id": nucc}, doc, upsert=True))
        if len(ops) >= batch_size:
            target_coll.bulk_write(ops, ordered=False)
            written += len(ops)
            ops = []
    if ops:
        target_coll.bulk_write(ops, ordered=False)
        written += len(ops)

    _log.LogPipeline(
        "INFO",
        "rollup_specialtymedicare: pairs=%d npis=%d nuccs=%d written=%d run_id=%s",
        len(acc), len(npis), len(by_nucc), written, run_id)
    return {"provider_molecule_pairs": len(acc), "distinct_npis": len(npis),
            "nucc_docs_written": written}
