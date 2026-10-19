# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""SpecialtyMedicare rollup engine (EPIC-010-F-007-S-009).

Rolls the flat provider x molecule collection up to the NUCC grain: for each
specialty, per molecule, the specialty_total (this NUCC's attributed usage) and
the de-duplicated molecule_real_total (the molecule's usage across all
specialties, each provider once) for reconciliation, plus prescriber_count.
provider_count is read from Provider_v_5 (every provider carrying the NUCC in
any taxonomy slot). The per-specialty embedding FindCare's semantic search uses
is copied from the SMD collection the provider pipeline already built.

The per-drug norms (means/averages) the evaluation compares a provider against
are computed at query time off the provider collection's (indications, npi,
molecule_id, quantities) covering index, not materialized here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    _ROLLUP_USAGE,
    _as_int,
    rollup_specialty_flat,
)

_log = ChatHealthyLoggingService()


def _specialty_embedding(smd_coll, nucc: str) -> dict:
    """Copy the specialty embedding the SMD collection already carries for this
    NUCC (the same vector FindCare's semantic search uses), rather than
    re-embedding."""
    if smd_coll is None:
        return {}
    doc = smd_coll.find_one({"_id": nucc}) or smd_coll.find_one({"nucc_code": nucc}) or {}
    emb = doc.get("embedding")
    if not emb:
        return {}
    out = {"embedding": emb}
    if doc.get("embedding_model"):
        out["embedding_model"] = doc["embedding_model"]
    return out


def rollup_specialtymedicare(
    *,
    run_id: str,
    data_version: int,
    providermedicare_coll,
    provider_spine_coll,
    target_coll,
    smd_coll=None,
    nucc_codes: set[str] | None = None,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Aggregate the flat provider x molecule collection into SpecialtyMedicare
    per NUCC. Reads every document for the run (molecule_real_total is a
    cross-specialty de-dup, so it needs the whole set) and emits the target
    NUCCs -- all of them in the single-partition placeholder, else the fan-out's
    nucc_codes."""
    agg = rollup_specialty_flat(
        providermedicare_coll.find({"provenance.source_run_id": run_id}))
    spec, real, seen_nuccs = agg["spec"], agg["real"], agg["nuccs"]

    by_nucc: dict[str, list] = {}
    for (nucc, mid), bucket in spec.items():
        by_nucc.setdefault(nucc, []).append((mid, bucket))

    emit = seen_nuccs if not nucc_codes else (seen_nuccs & set(nucc_codes))
    built_at = datetime.now(timezone.utc).isoformat()
    provenance = {
        "aggregated_from": [providermedicare_coll.name],
        "run_id": run_id,
        "built_at": built_at,
    }

    ops: list[UpdateOne] = []
    written = 0
    for nucc in emit:
        molecules = []
        for mid, bucket in by_nucc.get(nucc, []):
            r = real.get(mid) or {}
            pc = len(bucket["providers"])
            rpc = len(r.get("providers") or [])
            molecules.append({
                "molecule_id": mid,
                "specialty_total": {k: _as_int(bucket[k]) for k in _ROLLUP_USAGE},
                # Per-measure mean across this specialty's prescribers of the
                # molecule -- the within-specialty norm a provider is scored on.
                "specialty_mean": {k: round(bucket[k] / pc, 4) if pc else None
                                   for k in _ROLLUP_USAGE},
                "molecule_real_total": {k: _as_int(r.get(k, 0)) for k in _ROLLUP_USAGE},
                # Per-measure mean across ALL prescribers of the molecule (each
                # provider once) -- the overall norm.
                "molecule_real_mean": {k: round(r.get(k, 0) / rpc, 4) if rpc else None
                                       for k in _ROLLUP_USAGE},
                "prescriber_count": pc,
                "real_prescriber_count": rpc,
            })
        provider_count = provider_spine_coll.count_documents({"taxonomies.code": nucc})
        doc_out = {
            "_id": nucc,
            "nucc_code": nucc,
            "molecules": molecules,
            "provider_count": provider_count,
            "provenance": provenance,
            **_specialty_embedding(smd_coll, nucc),
        }
        ops.append(UpdateOne({"_id": nucc}, {"$set": doc_out}, upsert=True))
        if len(ops) >= batch_size:
            target_coll.bulk_write(ops, ordered=False)
            written += len(ops)
            ops = []
    if ops:
        target_coll.bulk_write(ops, ordered=False)
        written += len(ops)

    _log.LogPipeline("INFO", "rollup_specialtymedicare: wrote %d NUCC docs run_id=%s",
                     written, run_id)
    return {"specialtymedicare_written": written, "nucc_count": len(emit)}
