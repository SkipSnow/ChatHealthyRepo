# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ProviderMedicare base build engine (EPIC-010-F-007).

Assembles one ProviderMedicare document per NPI for a provider_state partition:
the provider spine (entity_type, specialty NUCC codes) from the built Provider
collection, the billed procedures from Part B staging, and the prescribed drugs
(with day supply) from Part D staging. rxcui / molecule / parent_indications on
drugs and allowed_icd10 / justification on procedures are left for the enrich
steps; this step lays down the base record per the ProviderMedicare contract.

CMS column names are isolated in the *_COLS maps below. They follow the CMS
public data dictionaries; first-fetch testing corrects any drift in one place.
Staging rows carry the verbatim source row under doc["raw"].
"""

from __future__ import annotations

from typing import Any

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

_log = ChatHealthyLoggingService()

# --- CMS Part D Prescribers by Provider and Drug (data-dictionary columns) ---
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

# --- CMS Part B Physician & Other Practitioners by Provider and Service -------
PARTB_COLS = {
    "npi": "Rndrng_NPI",
    "state": "Rndrng_Prvdr_State_Abrvtn",
    "entity": "Rndrng_Prvdr_Ent_Cd",       # "I" | "O"
    "hcpcs": "HCPCS_Cd",
    "descriptor": "HCPCS_Desc",
    "service_count": "Tot_Srvcs",
    "beneficiary_count": "Tot_Benes",
    "submitted_amount": "Avg_Sbmtd_Chrg",
    "allowed_amount": "Avg_Mdcr_Alowd_Amt",
    "paid_amount": "Avg_Mdcr_Pymt_Amt",
}


def _num(val: Any) -> float | int | None:
    if val is None or val == "":
        return None
    try:
        f = float(str(val).replace(",", ""))
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


def _raw_rows(coll, run_id: str, state_col: str, state: str):
    """Staged rows for this run whose source state column matches the state."""
    cursor = coll.find({"run_id": run_id, f"raw.{state_col}": state})
    for doc in cursor:
        yield doc.get("raw") or {}


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


def build_providermedicare(
    *,
    state: str,
    run_id: str,
    data_version: int,
    mongo,
    provider_spine_coll,
    partb_coll,
    partd_coll,
    target_coll,
    spine_state_filter: dict,
    batch_size: int = 1000,
) -> dict[str, Any]:
    """Build ProviderMedicare docs for one provider_state into target_coll."""
    # Gather this state's Part B procedures and Part D drugs, keyed by NPI.
    procedures_by_npi: dict[str, list[dict]] = {}
    for raw in _raw_rows(partb_coll, run_id, PARTB_COLS["state"], state):
        npi = str(raw.get(PARTB_COLS["npi"]) or "").strip()
        if not npi:
            continue
        procedures_by_npi.setdefault(npi, []).append({
            "hcpcs": raw.get(PARTB_COLS["hcpcs"]),
            "descriptor": raw.get(PARTB_COLS["descriptor"]),
            "hcpcs_level2": None,
            "label": None,
            "service_count": _num(raw.get(PARTB_COLS["service_count"])),
            "beneficiary_count": _num(raw.get(PARTB_COLS["beneficiary_count"])),
            "submitted_amount": _num(raw.get(PARTB_COLS["submitted_amount"])),
            "allowed_amount": _num(raw.get(PARTB_COLS["allowed_amount"])),
            "paid_amount": _num(raw.get(PARTB_COLS["paid_amount"])),
            "allowed_icd10": [],
            "justification_kind": "",
            "justification_source": "",
        })

    drugs_by_npi: dict[str, list[dict]] = {}
    for raw in _raw_rows(partd_coll, run_id, PARTD_COLS["state"], state):
        npi = str(raw.get(PARTD_COLS["npi"]) or "").strip()
        if not npi:
            continue
        drugs_by_npi.setdefault(npi, []).append({
            "brand": raw.get(PARTD_COLS["brand"]),
            "generic": raw.get(PARTD_COLS["generic"]),
            "total_claims": _num(raw.get(PARTD_COLS["total_claims"])),
            "days_supply": _num(raw.get(PARTD_COLS["days_supply"])),
            "beneficiary_count": _num(raw.get(PARTD_COLS["beneficiary_count"])),
            "total_drug_cost": _num(raw.get(PARTD_COLS["total_drug_cost"])),
            "rxcui": None,
            "molecule": None,
            "parent_indications": [],
        })

    # One document per NPI present in the spine for this state.
    ops: list[UpdateOne] = []
    written = 0
    npis_in_state = set(procedures_by_npi) | set(drugs_by_npi)
    for spine in provider_spine_coll.find(spine_state_filter):
        npi = str(spine.get("npi") or spine.get("_id") or "").strip()
        if not npi:
            continue
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
        npis_in_state.discard(npi)
        if len(ops) >= batch_size:
            target_coll.bulk_write(ops, ordered=False)
            written += len(ops)
            ops = []
    if ops:
        target_coll.bulk_write(ops, ordered=False)
        written += len(ops)

    _log.LogPipeline(
        "INFO",
        "build_providermedicare[%s]: wrote=%d billed_only_npis_without_spine=%d",
        state, written, len(npis_in_state),
    )
    return {
        "state": state,
        "providermedicare_written": written,
        "billed_npis_without_spine": len(npis_in_state),
    }
