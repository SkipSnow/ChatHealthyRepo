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


def _nucc_map(provider_spine_coll, npis: list[str], chunk: int = 5000) -> dict[str, list[str]]:
    """npi -> distinct NUCC codes from the Provider spine's taxonomies."""
    out: dict[str, list[str]] = {}
    for i in range(0, len(npis), chunk):
        for doc in provider_spine_coll.find(
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


def rollup_specialtymedicare(
    *,
    run_id: str,
    partd_coll,
    provider_spine_coll,
    target_coll,
    batch_size: int = 200,
) -> dict[str, Any]:
    """Build specialtyMedicarePartDQuantityRollUps for the run (serial)."""
    projection = {f"raw.{PARTD_COLS[k]}": 1 for k in
                  ("npi", "generic", "total_claims", "total_30day_fills",
                   "days_supply", "total_drug_cost")}
    rows = (doc.get("raw") or {}
            for doc in partd_coll.find({"run_id": run_id}, projection))
    acc = accumulate_provider_molecule_totals(rows)

    npis = sorted({npi for npi, _ in acc})
    nucc_by_npi = _nucc_map(provider_spine_coll, npis)
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
            "provider_count": provider_spine_coll.count_documents(
                {"taxonomies.code": nucc}),
            "provenance": {
                "aggregated_from": ["StagingMedicarePartD_v_1", "Provider_v_5"],
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
