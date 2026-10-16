# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Pure normalization + aggregation of denormalized CMS rows (EPIC-010-F-007).

The CMS Part B ("by Provider and Service") and Part D ("by Provider and Drug")
files are denormalized and joint-keyed: many rows per NPI. Our collections are
produced by NORMALIZING and AGGREGATING those rows up to our target grain.

Everything here is a PURE FUNCTION over row data -- input is lists of raw CMS
row dicts (or built provider-doc dicts for the rollup core), output is grained
records. No Mongo, no I/O, no global state: the engines feed these functions
rows fetched from staging and write the results. This is the layer the unit
tests exercise directly, in memory, with fabricated CMS rows.

CMS column names follow the official CMS public data dictionaries and are the
single source of truth for the engines that import them.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

# --- CMS Part D "Prescribers by Provider and Drug" ---------------------------
# Record grain in the source: Prscrbr_NPI + Brnd_Name + Gnrc_Name.
PARTD_COLS = {
    "npi": "Prscrbr_NPI",
    "state": "Prscrbr_State_Abrvtn",
    "brand": "Brnd_Name",
    "generic": "Gnrc_Name",
    "total_claims": "Tot_Clms",
    "days_supply": "Tot_Day_Suply",
    "beneficiary_count": "Tot_Benes",
    "total_drug_cost": "Tot_Drug_Cst",
}

# --- CMS Part B "Physician & Other Practitioners by Provider and Service" -----
# Record grain in the source: Rndrng_NPI + HCPCS_Cd + Place_Of_Srvc (F | O).
PARTB_COLS = {
    "npi": "Rndrng_NPI",
    "state": "Rndrng_Prvdr_State_Abrvtn",
    "entity": "Rndrng_Prvdr_Ent_Cd",       # "I" | "O"
    "hcpcs": "HCPCS_Cd",
    "descriptor": "HCPCS_Desc",
    "place_of_service": "Place_Of_Srvc",    # "F" facility | "O" non-facility
    "service_count": "Tot_Srvcs",
    "beneficiary_count": "Tot_Benes",
    "submitted_amount": "Avg_Sbmtd_Chrg",
    "allowed_amount": "Avg_Mdcr_Alowd_Amt",
    "paid_amount": "Avg_Mdcr_Pymt_Amt",
}


def _num(val: Any) -> float | int | None:
    """Parse a CMS numeric cell to int (when integral) or float; None if blank."""
    if val is None or val == "":
        return None
    try:
        f = float(str(val).replace(",", ""))
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


def _npi(raw: dict, col: str) -> str:
    return str(raw.get(col) or "").strip()


def _sum_opt(a: Any, b: Any) -> int | float | None:
    """Sum two optional numerics; None only when BOTH are absent."""
    na, nb = _num(a), _num(b)
    if na is None and nb is None:
        return None
    total = (na or 0) + (nb or 0)
    return int(total) if float(total).is_integer() else total


def aggregate_procedures(partb_rows: Iterable[dict]) -> dict[str, list[dict]]:
    """Part B rows -> one procedure per (NPI, HCPCS), grouped by NPI.

    The facility / non-facility place-of-service split (Place_Of_Srvc F vs O) is
    COLLAPSED into a single procedure per HCPCS. Tot_Srvcs and Tot_Benes are
    SUMMED across the two places of service. The dollar amounts
    (Avg_Sbmtd_Chrg / Avg_Mdcr_Alowd_Amt / Avg_Mdcr_Pymt_Amt) are carried
    SEPARATELY as facility_* and nonfacility_* (lossless -- the two fee
    schedules are genuinely different and must not be averaged).
    """
    C = PARTB_COLS
    # (npi, hcpcs) -> accumulating procedure
    by_key: dict[tuple[str, str], dict] = {}
    order: dict[str, list[str]] = defaultdict(list)  # npi -> hcpcs in first-seen order
    for raw in partb_rows:
        npi = _npi(raw, C["npi"])
        hcpcs = str(raw.get(C["hcpcs"]) or "").strip()
        if not npi or not hcpcs:
            continue
        key = (npi, hcpcs)
        proc = by_key.get(key)
        if proc is None:
            proc = {
                "hcpcs": hcpcs,
                "descriptor": raw.get(C["descriptor"]),
                "hcpcs_level2": None,
                "label": None,
                "service_count": None,
                "beneficiary_count": None,
                "facility_submitted_amount": None,
                "facility_allowed_amount": None,
                "facility_paid_amount": None,
                "nonfacility_submitted_amount": None,
                "nonfacility_allowed_amount": None,
                "nonfacility_paid_amount": None,
                "allowed_icd10": [],
                "justification_kind": "",
                "justification_source": "",
            }
            by_key[key] = proc
            order[npi].append(hcpcs)
        if not proc["descriptor"] and raw.get(C["descriptor"]):
            proc["descriptor"] = raw.get(C["descriptor"])
        proc["service_count"] = _sum_opt(proc["service_count"], raw.get(C["service_count"]))
        proc["beneficiary_count"] = _sum_opt(proc["beneficiary_count"], raw.get(C["beneficiary_count"]))
        pos = str(raw.get(C["place_of_service"]) or "").strip().upper()
        prefix = "facility" if pos == "F" else "nonfacility"
        proc[f"{prefix}_submitted_amount"] = _num(raw.get(C["submitted_amount"]))
        proc[f"{prefix}_allowed_amount"] = _num(raw.get(C["allowed_amount"]))
        proc[f"{prefix}_paid_amount"] = _num(raw.get(C["paid_amount"]))

    out: dict[str, list[dict]] = {}
    for npi, hcpcs_list in order.items():
        out[npi] = [by_key[(npi, hc)] for hc in hcpcs_list]
    return out


def collect_drugs(partd_rows: Iterable[dict]) -> dict[str, list[dict]]:
    """Part D rows -> raw drug entries grouped by NPI, one entry per source row.

    The base build keeps drug rows RAW (one per brand/generic row). The RXCUI
    re-grain (brand+generic -> one per RXCUI) happens in the enrich pass, where
    RXCUI resolution already lives -- see regrain_drugs_by_rxcui.
    """
    C = PARTD_COLS
    out: dict[str, list[dict]] = defaultdict(list)
    for raw in partd_rows:
        npi = _npi(raw, C["npi"])
        if not npi:
            continue
        out[npi].append({
            "brand": raw.get(C["brand"]),
            "generic": raw.get(C["generic"]),
            "total_claims": _num(raw.get(C["total_claims"])),
            "days_supply": _num(raw.get(C["days_supply"])),
            "beneficiary_count": _num(raw.get(C["beneficiary_count"])),
            "total_drug_cost": _num(raw.get(C["total_drug_cost"])),
            "rxcui": None,
            "molecule": None,
            "parent_indications": [],
        })
    return dict(out)


def distinct_billed_npis(
    partb_rows: Iterable[dict], partd_rows: Iterable[dict]
) -> set[str]:
    """The distinct set of billed (Part B) + prescribing (Part D) NPIs."""
    npis: set[str] = set()
    for raw in partb_rows:
        npi = _npi(raw, PARTB_COLS["npi"])
        if npi:
            npis.add(npi)
    for raw in partd_rows:
        npi = _npi(raw, PARTD_COLS["npi"])
        if npi:
            npis.add(npi)
    return npis


def npis_not_found(
    billed_npis: Iterable[str], provider_npis: Iterable[str]
) -> list[str]:
    """Distinct billed/prescribing NPIs absent from the Provider collection.

    De-duped by construction: a single NPI spanning many drug/procedure/source
    rows contributes exactly one entry, because the input is a set difference.
    Returns a sorted list so the finding order is deterministic.
    """
    return sorted(set(billed_npis) - set(provider_npis))


def _drug_grain_key(drug: dict) -> str:
    """Grain key for the RXCUI re-grain. Prefer RXCUI; fall back to the
    normalized generic (then brand) name when no RXCUI resolved -- a drug with
    no RXCUI cannot be given one, so it collapses by molecule name instead."""
    rx = str(drug.get("rxcui") or "").strip()
    if rx:
        return f"rxcui:{rx}"
    gen = str(drug.get("generic") or "").strip().lower()
    if gen:
        return f"gnrc:{gen}"
    brand = str(drug.get("brand") or "").strip().lower()
    return f"brnd:{brand}" if brand else "unknown:"


def regrain_drugs_by_rxcui(drugs: Iterable[dict]) -> list[dict]:
    """Collapse drug entries sharing an RXCUI into exactly one per RXCUI.

    SUMS Tot_Clms / Tot_Day_Suply / Tot_Benes / Tot_Drug_Cst across the
    collapsed brand/generic rows (decision: sums, not averages). Molecule is
    carried through; parent_indications are unioned. When several distinct brand
    names collapse onto one RXCUI the brand becomes ambiguous and is dropped to
    None; a single surviving brand is kept.
    """
    acc: dict[str, dict] = {}
    order: list[str] = []
    for d in drugs:
        key = _drug_grain_key(d)
        cur = acc.get(key)
        if cur is None:
            cur = {
                "brand": d.get("brand"),
                "generic": d.get("generic"),
                "total_claims": None,
                "days_supply": None,
                "beneficiary_count": None,
                "total_drug_cost": None,
                "rxcui": d.get("rxcui"),
                "molecule": d.get("molecule"),
                "parent_indications": list(d.get("parent_indications") or []),
                "_brands": set(),
            }
            acc[key] = cur
            order.append(key)
        b = d.get("brand")
        if b:
            cur["_brands"].add(str(b))
        if not cur.get("generic") and d.get("generic"):
            cur["generic"] = d.get("generic")
        if not cur.get("molecule") and d.get("molecule"):
            cur["molecule"] = d.get("molecule")
        if not cur.get("rxcui") and d.get("rxcui"):
            cur["rxcui"] = d.get("rxcui")
        cur["total_claims"] = _sum_opt(cur["total_claims"], d.get("total_claims"))
        cur["days_supply"] = _sum_opt(cur["days_supply"], d.get("days_supply"))
        cur["beneficiary_count"] = _sum_opt(cur["beneficiary_count"], d.get("beneficiary_count"))
        cur["total_drug_cost"] = _sum_opt(cur["total_drug_cost"], d.get("total_drug_cost"))
        for iid in d.get("parent_indications") or []:
            if iid not in cur["parent_indications"]:
                cur["parent_indications"].append(iid)

    out: list[dict] = []
    for key in order:
        cur = acc[key]
        brands = cur.pop("_brands")
        if len(brands) > 1:
            cur["brand"] = None
        elif len(brands) == 1 and not cur.get("brand"):
            cur["brand"] = next(iter(brands))
        cur["parent_indications"] = sorted(cur["parent_indications"])
        out.append(cur)
    return out


def rollup_specialty(
    provider_docs: Iterable[dict], nucc_codes: set[str] | None = None
) -> dict[str, dict]:
    """Pure core of the SpecialtyMedicare rollup over built ProviderMedicare docs.

    For each NUCC a provider carries (every code in its specialty[]), aggregate:
      * procedures -- one per (HCPCS, NUCC): provider set, summed service/
        beneficiary counts, summed allowed amount (facility + non-facility).
      * drugs -- one per (RXCUI, NUCC): provider set, summed claims/benes/cost,
        and the per-provider day-supply list (the input to the benchmark stats).
      * indications -- one per (indication_id, NUCC): per-provider day-supply.

    Returns per NUCC an intermediate aggregate; the engine turns the day-supply
    lists into mean/median/quintiles and writes the documents. Sets are returned
    so the engine can take their lengths for provider_count.
    """
    out: dict[str, dict] = {}

    def _nucc(code: str) -> dict:
        node = out.get(code)
        if node is None:
            node = {
                "procedures": {},        # hcpcs -> agg
                "drugs": {},             # drug-key -> agg
                "indications": {},       # iid -> {"days_supply_list": [...]}
                "indications_served": set(),
                "seen_providers": set(),
            }
            out[code] = node
        return node

    for doc in provider_docs:
        npi = doc.get("_id") or doc.get("npi")
        for code in doc.get("specialty") or []:
            if nucc_codes is not None and code not in nucc_codes:
                continue
            node = _nucc(code)
            node["seen_providers"].add(npi)

            # drugs -> one per (RXCUI, NUCC); day supply summed per provider
            prov_drug_ds: dict[str, float] = defaultdict(float)
            for d in doc.get("drugs") or []:
                key = d.get("rxcui") or d.get("generic") or d.get("brand")
                if not key:
                    continue
                agg = node["drugs"].setdefault(key, {
                    "rxcui": d.get("rxcui"),
                    "molecule": d.get("molecule"),
                    "generic": d.get("generic"),
                    "provider_npis": set(),
                    "total_claims": 0,
                    "beneficiary_count": 0,
                    "total_cost": 0.0,
                    "days_supply_list": [],
                })
                agg["provider_npis"].add(npi)
                agg["total_claims"] += int(_num(d.get("total_claims")) or 0)
                agg["beneficiary_count"] += int(_num(d.get("beneficiary_count")) or 0)
                agg["total_cost"] += float(_num(d.get("total_drug_cost")) or 0.0)
                if not agg.get("rxcui") and d.get("rxcui"):
                    agg["rxcui"] = d.get("rxcui")
                if not agg.get("molecule") and d.get("molecule"):
                    agg["molecule"] = d.get("molecule")
                ds = _num(d.get("days_supply"))
                if ds is not None:
                    prov_drug_ds[key] += ds
            for key, ds in prov_drug_ds.items():
                node["drugs"][key]["days_supply_list"].append(ds)

            # indications -> one per (indication_id, NUCC); day supply per provider
            prov_ind_ds: dict[str, float] = defaultdict(float)
            for ind in doc.get("indications") or []:
                iid = ind.get("indication_id")
                if not iid:
                    continue
                node["indications_served"].add(iid)
                ds = _num(ind.get("days_supply"))
                if ds is not None:
                    prov_ind_ds[iid] += ds
            for iid, ds in prov_ind_ds.items():
                node["indications"].setdefault(iid, {"days_supply_list": []})
                node["indications"][iid]["days_supply_list"].append(ds)

            # procedures -> one per (HCPCS, NUCC)
            for p in doc.get("procedures") or []:
                hc = p.get("hcpcs")
                if not hc:
                    continue
                agg = node["procedures"].setdefault(hc, {
                    "hcpcs": hc,
                    "provider_npis": set(),
                    "service_count": 0,
                    "beneficiary_count": 0,
                    "allowed_amount": 0.0,
                })
                agg["provider_npis"].add(npi)
                agg["service_count"] += int(_num(p.get("service_count")) or 0)
                agg["beneficiary_count"] += int(_num(p.get("beneficiary_count")) or 0)
                allowed = (float(_num(p.get("facility_allowed_amount")) or 0.0)
                           + float(_num(p.get("nonfacility_allowed_amount")) or 0.0))
                agg["allowed_amount"] += allowed

    return out
