# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""SpecialtyMedicare rollup engine (EPIC-010-F-007).

A rollup of the finished ProviderMedicare. For each NUCC code it aggregates the
specialty's procedures, drugs and indications, and computes the day-supply mean,
median and four quintile cut-points per drug and per indication across the
providers in that specialty (the cut-points define the five tiers a provider is
placed into at evaluation). provider_count is read from the provider base (a
provider counts under every NUCC in any taxonomy slot). The benchmark figures
live only on SpecialtyMedicare, never on provider records.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

_log = ChatHealthyLoggingService()


def _num(v: Any) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _percentile(sorted_vals: list[float], p: float) -> float:
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    idx = p * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def _stats(values: Iterable[float]) -> dict[str, Any]:
    vals = sorted(v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool))
    n = len(vals)
    if n == 0:
        return {"mean": None, "median": None, "quintiles": None}
    mean = sum(vals) / n
    return {
        "mean": round(mean, 4),
        "median": round(_percentile(vals, 0.5), 4),
        "quintiles": [round(_percentile(vals, p), 4) for p in (0.2, 0.4, 0.6, 0.8)],
    }


def _specialty_embedding(smd_coll, nucc: str) -> dict:
    """Copy the specialty embedding the SMD collection already carries for this
    NUCC (same vector FindCare's vector search uses), rather than re-embedding."""
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
    """Aggregate ProviderMedicare into SpecialtyMedicare per NUCC."""
    # Per NUCC: per-provider day-supply lists keyed by drug and by indication,
    # plus running procedure/drug aggregates and the set of providers seen.
    drug_ds: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    ind_ds: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    drug_meta: dict[str, dict[str, dict]] = defaultdict(dict)
    proc_agg: dict[str, dict[str, dict]] = defaultdict(dict)
    inds_served: dict[str, set] = defaultdict(set)
    seen_providers: dict[str, set] = defaultdict(set)

    query: dict[str, Any] = {"run_id": run_id}
    if nucc_codes:
        query["specialty"] = {"$in": list(nucc_codes)}

    for doc in providermedicare_coll.find(query):
        npi = doc.get("_id")
        for nucc in doc.get("specialty") or []:
            if nucc_codes and nucc not in nucc_codes:
                continue
            seen_providers[nucc].add(npi)

            prov_drug: dict[str, float] = defaultdict(float)
            for d in doc.get("drugs") or []:
                key = d.get("rxcui") or d.get("generic") or d.get("brand")
                if not key:
                    continue
                ds = _num(d.get("days_supply"))
                if ds is not None:
                    prov_drug[key] += ds
                meta = drug_meta[nucc].setdefault(key, {
                    "rxcui": d.get("rxcui"), "molecule": d.get("molecule"),
                    "generic": d.get("generic"), "provider_npis": set(),
                    "total_claims": 0, "beneficiary_count": 0, "total_cost": 0.0,
                })
                meta["provider_npis"].add(npi)
                meta["total_claims"] += int(_num(d.get("total_claims")) or 0)
                meta["beneficiary_count"] += int(_num(d.get("beneficiary_count")) or 0)
                meta["total_cost"] += float(_num(d.get("total_drug_cost")) or 0.0)
            for key, ds in prov_drug.items():
                drug_ds[nucc][key].append(ds)

            prov_ind: dict[str, float] = defaultdict(float)
            for ind in doc.get("indications") or []:
                iid = ind.get("indication_id")
                if not iid:
                    continue
                inds_served[nucc].add(iid)
                ds = _num(ind.get("days_supply"))
                if ds is not None:
                    prov_ind[iid] += ds
            for iid, ds in prov_ind.items():
                ind_ds[nucc][iid].append(ds)

            for p in doc.get("procedures") or []:
                hc = p.get("hcpcs")
                if not hc:
                    continue
                agg = proc_agg[nucc].setdefault(hc, {
                    "hcpcs": hc, "provider_npis": set(),
                    "service_count": 0, "beneficiary_count": 0, "allowed_amount": 0.0,
                })
                agg["provider_npis"].add(npi)
                agg["service_count"] += int(_num(p.get("service_count")) or 0)
                agg["beneficiary_count"] += int(_num(p.get("beneficiary_count")) or 0)
                agg["allowed_amount"] += float(_num(p.get("allowed_amount")) or 0.0)

    ops: list[UpdateOne] = []
    written = 0
    all_nuccs = set(seen_providers) | set(drug_ds) | set(ind_ds) | set(proc_agg)
    for nucc in all_nuccs:
        drugs_out = []
        for key, meta in drug_meta.get(nucc, {}).items():
            s = _stats(drug_ds[nucc].get(key, []))
            drug = {
                "molecule": meta["molecule"] or meta["generic"],
                "provider_count": len(meta["provider_npis"]),
                "total_claims": meta["total_claims"],
                "beneficiary_count": meta["beneficiary_count"],
                "total_cost": round(meta["total_cost"], 4),
                "days_supply_mean": s["mean"], "days_supply_median": s["median"],
            }
            if meta["rxcui"]:
                drug["rxcui"] = meta["rxcui"]
            if s["quintiles"] is not None:
                drug["days_supply_quintiles"] = s["quintiles"]
            drugs_out.append(drug)
        inds_out = []
        for iid, vals in ind_ds.get(nucc, {}).items():
            s = _stats(vals)
            ind = {
                "indication_id": iid, "provider_count": len(vals),
                "days_supply_mean": s["mean"], "days_supply_median": s["median"],
            }
            if s["quintiles"] is not None:
                ind["days_supply_quintiles"] = s["quintiles"]
            inds_out.append(ind)
        procs_out = [{
            "hcpcs": a["hcpcs"], "provider_count": len(a["provider_npis"]),
            "service_count": a["service_count"], "beneficiary_count": a["beneficiary_count"],
            "allowed_amount": round(a["allowed_amount"], 4),
        } for a in proc_agg.get(nucc, {}).values()]

        # provider_count: every provider in the base carrying this NUCC in ANY
        # taxonomy slot (not only providers present in the Medicare rollup).
        base_provider_count = provider_spine_coll.count_documents({"taxonomies.code": nucc})

        doc_out = {
            "_id": nucc, "nucc_code": nucc,
            "provider_count": base_provider_count,
            "run_id": run_id, "data_version": data_version,
            "procedures": procs_out, "drugs": drugs_out, "indications": inds_out,
            "indications_served": sorted(inds_served.get(nucc, set())),
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

    _log.LogPipeline("INFO", "rollup_specialtymedicare: wrote %d NUCC docs run_id=%s", written, run_id)
    return {"specialtymedicare_written": written, "nucc_count": len(all_nuccs)}
