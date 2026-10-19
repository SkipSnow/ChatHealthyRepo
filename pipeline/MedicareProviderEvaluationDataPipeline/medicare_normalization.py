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

import hashlib
from collections import defaultdict
from typing import Any, Callable, Iterable

# --- CMS Part D "Prescribers by Provider and Drug" ---------------------------
# Record grain in the source: Prscrbr_NPI + Brnd_Name + Gnrc_Name.
PARTD_COLS = {
    "npi": "Prscrbr_NPI",
    "state": "Prscrbr_State_Abrvtn",
    "brand": "Brnd_Name",
    "generic": "Gnrc_Name",
    # all-ages usage (never suppressed) + all-ages beneficiaries (suppressible)
    "total_claims": "Tot_Clms",
    "total_30day_fills": "Tot_30day_Fills",
    "days_supply": "Tot_Day_Suply",
    "total_drug_cost": "Tot_Drug_Cst",
    "beneficiary_count": "Tot_Benes",
    # 65-and-over usage + beneficiaries (all-or-nothing under GE65_Sprsn_Flag)
    "ge65_total_claims": "GE65_Tot_Clms",
    "ge65_total_30day_fills": "GE65_Tot_30day_Fills",
    "ge65_days_supply": "GE65_Tot_Day_Suply",
    "ge65_total_drug_cost": "GE65_Tot_Drug_Cst",
    "ge65_beneficiary_count": "GE65_Tot_Benes",
    # suppression flags (present on the row only when the value is suppressed)
    "bene_sprsn_flag": "Bene_Sprsn_Flag",
    "ge65_sprsn_flag": "GE65_Sprsn_Flag",
    "ge65_bene_sprsn_flag": "GE65_Bene_Sprsn_Flag",
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

    The base build keeps drug rows RAW (one per brand/generic row). The brand
    re-grain (the provider's rows -> one entry per NORMALIZED brand, joined to the
    FDA drug collection for molecule/rxcui-set/indications) happens in the enrich
    pass -- see compose_drugs_by_brand. The raw entry already carries the empty
    molecule/rxcuis/indications slots so the base-written document conforms to
    ChatHealthyProviderMedicareSchema before enrichment fills them.
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
            "molecule": None,
            "rxcuis": [],
            "indications": [],
        })
    return dict(out)


def _sprsn(raw: dict, col: str) -> bool:
    """A CMS suppression flag is present (non-empty) only when the value is
    suppressed; absent/blank means visible. (Marker string confirmed against a
    staging sample before the run.)"""
    return bool(str(raw.get(col) or "").strip())


def partd_child(raw: dict) -> dict:
    """One RAW per-CMS-record child for the molecule master (III.2.2 / III.7).

    All-ages usage (tot_clms/30day/day_suply/drug_cst) is never suppressed and is
    carried as given -- it is what the master totals sum. A suppressed quantity is
    carried as None with its flag kept, never 0 and never reconstructed:
    all-ages beneficiaries under Bene_Sprsn_Flag; the four GE65 usage quantities
    all-or-nothing under GE65_Sprsn_Flag; GE65 beneficiaries under
    GE65_Bene_Sprsn_Flag. Beneficiaries are never summed. nucc_codes is filled by
    the engine from Provider_v_5 (by npi); CMS Prscrbr_Type is not carried."""
    C = PARTD_COLS
    bene_sup = _sprsn(raw, C["bene_sprsn_flag"])
    ge65_sup = _sprsn(raw, C["ge65_sprsn_flag"])
    ge65_bene_sup = _sprsn(raw, C["ge65_bene_sprsn_flag"])
    return {
        "npi": _npi(raw, C["npi"]),
        "state": str(raw.get(C["state"]) or "").strip(),
        "nucc_codes": [],
        "brnd_name": raw.get(C["brand"]),
        "gnrc_name": raw.get(C["generic"]),
        "tot_clms": _num(raw.get(C["total_claims"])),
        "tot_30day_fills": _num(raw.get(C["total_30day_fills"])),
        "tot_day_suply": _num(raw.get(C["days_supply"])),
        "tot_drug_cst": _num(raw.get(C["total_drug_cost"])),
        "tot_benes": None if bene_sup else _num(raw.get(C["beneficiary_count"])),
        "ge65_tot_clms": None if ge65_sup else _num(raw.get(C["ge65_total_claims"])),
        "ge65_tot_30day_fills": None if ge65_sup else _num(raw.get(C["ge65_total_30day_fills"])),
        "ge65_tot_day_suply": None if ge65_sup else _num(raw.get(C["ge65_days_supply"])),
        "ge65_tot_drug_cst": None if ge65_sup else _num(raw.get(C["ge65_total_drug_cost"])),
        "ge65_tot_benes": None if ge65_bene_sup else _num(raw.get(C["ge65_beneficiary_count"])),
        "suppression_flags": {
            "Bene_Sprsn_Flag": raw.get(C["bene_sprsn_flag"]),
            "GE65_Sprsn_Flag": raw.get(C["ge65_sprsn_flag"]),
            "GE65_Bene_Sprsn_Flag": raw.get(C["ge65_bene_sprsn_flag"]),
        },
    }


def child_ge65_suppressed(child: dict) -> bool:
    """A raw child's 65+ usage was suppressed (GE65_Sprsn_Flag present)."""
    return bool(str((child.get("suppression_flags") or {}).get("GE65_Sprsn_Flag") or "").strip())


_ALLAGES_USAGE = ("tot_clms", "tot_30day_fills", "tot_day_suply", "tot_drug_cst")
_GE65_TO_PLAIN = {
    "ge65_tot_clms": "tot_clms",
    "ge65_tot_30day_fills": "tot_30day_fills",
    "ge65_tot_day_suply": "tot_day_suply",
    "ge65_tot_drug_cst": "tot_drug_cst",
}


def _as_int(v: Any) -> int | float:
    return int(v) if isinstance(v, float) and v.is_integer() else v


def aggregate_provider_molecule(rows: Iterable[dict]) -> dict:
    """Aggregate ONE provider's CMS Part D rows for ONE molecule into the flat
    (provider x molecule) record's quantity block (EPIC-010-F-007, provider-core
    model). The provider is the core object; this is one molecule under it.

    Usage (claims / 30-day fills / day-supply / cost) is ADDITIVE -> summed over
    the provider's brand rows for the molecule. GE65 usage sums the VISIBLE rows
    only; usage_suppressed_count tallies the rows CMS suppressed (GE65_Sprsn_Flag
    is all-or-nothing). Beneficiaries are NON-ADDITIVE (summing double-counts
    patients across brands) -> carried only when a single unsuppressed row backs
    the pair, else null. brands is the distinct brand set; record_count is the
    CMS rows behind the pair (the conformance unit)."""
    children = [partd_child(r) for r in rows]
    totals = {k: 0 for k in _ALLAGES_USAGE}
    ge65 = {v: 0 for v in _GE65_TO_PLAIN.values()}
    ge65_suppressed = 0
    brands: list[str] = []
    seen: set = set()
    for c in children:
        for k in _ALLAGES_USAGE:
            if c[k] is not None:
                totals[k] += c[k]
        if child_ge65_suppressed(c):
            ge65_suppressed += 1
        else:
            for src, dst in _GE65_TO_PLAIN.items():
                if c[src] is not None:
                    ge65[dst] += c[src]
        brand = str(c["brnd_name"] or "").strip()
        if brand and brand not in seen:
            seen.add(brand)
            brands.append(brand)
    if len(children) == 1:
        tot_benes = children[0]["tot_benes"]
        ge65_tot_benes = children[0]["ge65_tot_benes"]
    else:
        tot_benes = None
        ge65_tot_benes = None
    return {
        "state": next((c["state"] for c in children if c["state"]), ""),
        "gnrc_name": children[0]["gnrc_name"] if children else None,
        "brands": brands,
        "record_count": len(children),
        "totals": {k: _as_int(v) for k, v in totals.items()},
        "ge65": {**{k: _as_int(v) for k, v in ge65.items()},
                 "usage_suppressed_count": ge65_suppressed},
        "benes": {"tot_benes": tot_benes, "ge65_tot_benes": ge65_tot_benes},
    }


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


# --- v1 drug-collection composition (brand join) -----------------------------
# A drug is identified in Part D ONLY by brand + generic name -- the CMS "by
# Provider and Drug" rows carry no rxcui/NDC. The composition therefore joins on
# the BRAND NAME, normalized on both the CMS and FDA sides with plain string
# operations only (NO regular expressions -- an engineering rule forbids them in
# pipeline code). One brand resolves to one molecule and one shared rxcui-set
# (repackager duplicates share the rxcui; the only multi-molecule cases are
# salt-spelling variants of one molecule), so unioning across every FDA record
# under a brand gives complete coverage once the phrasing variants are deduped.

# Salt and dose-form suffixes stripped from the brand JOIN KEY when they would
# otherwise block a match. Applied symmetrically to both sides, so a CMS
# "LIPITOR ER" and an FDA "LIPITOR" collapse to the same key. Conservative and
# data-driven: a trailing token is dropped only when it is one of these.
_BRAND_SALT_SUFFIXES = frozenset({
    "HCL", "HYDROCHLORIDE", "SULFATE", "SODIUM", "CALCIUM", "POTASSIUM",
    "HBR", "HYDROBROMIDE", "MESYLATE", "MALEATE", "TARTRATE", "BITARTRATE",
    "CITRATE", "FUMARATE", "ACETATE", "PHOSPHATE", "SUCCINATE", "BESYLATE",
    "NITRATE", "CHLORIDE",
})
_BRAND_FORM_SUFFIXES = frozenset({"ER", "XR", "SR", "CR", "XL", "DR", "ODT", "EC"})


def normalize_brand(name: Any) -> str:
    """Plain-string brand normalization (NO regex): uppercase, collapse internal
    whitespace, and reconcile the two spellings of a combination drug -- "X AND Y"
    and "X - Y" both become "X-Y". Salt and dose-form suffixes are NOT stripped
    here; that is brand_join_key's job. This is the display/grouping form carried
    on the composed record as `brand`."""
    s = " ".join(str(name or "").upper().split())
    if not s:
        return ""
    s = s.replace(" AND ", "-")
    # Normalize spacing around hyphens so "X - Y" and "X-Y" agree.
    s = s.replace(" - ", "-").replace("- ", "-").replace(" -", "-")
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-").strip()


def brand_join_key(name: Any) -> str:
    """The canonical BRAND JOIN KEY used on BOTH the CMS and FDA sides (NO regex).
    It is normalize_brand with trailing salt/form suffix tokens removed, so the
    same drug written "ATORVASTATIN CALCIUM" / "ATORVASTATIN" / "LIPITOR ER" joins
    across the two sources. At least one token is always kept."""
    base = normalize_brand(name)
    if not base:
        return ""
    tokens = base.split(" ")
    while len(tokens) > 1 and (
            tokens[-1] in _BRAND_SALT_SUFFIXES or tokens[-1] in _BRAND_FORM_SUFFIXES):
        tokens.pop()
    return " ".join(tokens)


def molecule_canonical(name: Any) -> str:
    """Canonical molecule name shared by the CMS Gnrc_Name and the recovered FDA
    clean name (NO regex): brand_join_key's case/whitespace-folded, salt/form-
    stripped form, so "ATORVASTATIN CALCIUM" (CMS) and "atorvastatin" (FDA) fold
    to one key. '' when empty."""
    return brand_join_key(name)


def molecule_id(name: Any) -> str | None:
    """The proprietary molecule surrogate: 'mol_' + a stable digest of the
    canonical molecule name. None when the name is empty. The SAME function runs
    on the FDA clean name and the CMS Gnrc_Name, so a molecule resolved from a
    label and the same molecule prescribed in CMS carry one id -- that id is the
    join key (FDA file carries it, the master is keyed on it). EPIC-010-F-007
    III.2.4."""
    canonical = molecule_canonical(name)
    if not canonical:
        return None
    return "mol_" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _norm_indication_text(text: Any) -> str:
    """Dedup key for an indication phrase: uppercase + whitespace-collapsed."""
    return " ".join(str(text or "").upper().split())


# FDA openfda sub-fields carried FLAT onto the brand entry (and thence each
# composed drug entry) as unioned, de-duplicated, first-seen-ordered lists per
# brand. The map is source `openfda.<field>` -> the flat key on the drug entry.
# brand_name is the join key's own source and is deliberately not carried as a
# payload field. openfda.rxcui is the full clinical-drug rxcui list, kept distinct
# from the ingredient-level `rxcuis`, under the flat key `clinical_rxcui`.
_OPENFDA_FLAT_FIELDS = {
    "generic_name": "generic_name",
    "substance_name": "substance_name",
    "product_type": "product_type",
    "rxcui": "clinical_rxcui",
    "product_ndc": "product_ndc",
    "application_number": "application_number",
    "spl_id": "spl_id",
    "spl_set_id": "spl_set_id",
}


def _extend_unique(dest: list, seen: set, values: Any) -> None:
    """Append each new stringified, stripped value in `values` to `dest`, keeping
    first-seen order and skipping blanks and duplicates. Accepts a scalar or list."""
    if isinstance(values, str):
        values = [values]
    for value in values or []:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            dest.append(text)


def _merge_indication(store: dict, order: list, ind: dict) -> None:
    """Union one FDA indication object into the per-brand indication store.

    Dedups by normalized indication text; on a repeat, UNIONS codes[] by
    icd10_code (keeping the richest set) and fills confidence_rationale if the
    first occurrence lacked one. The full code object is preserved
    (icd10_code, level, confidence, description)."""
    text = str((ind or {}).get("indication") or "").strip()
    if not text:
        return
    norm = _norm_indication_text(text)
    if not norm:
        return
    entry = store.get(norm)
    if entry is None:
        entry = {
            "indication": text,
            "confidence_rationale": str((ind or {}).get("confidence_rationale") or "").strip() or None,
            "codes": [],
            "_code_keys": set(),
        }
        store[norm] = entry
        order.append(norm)
    elif not entry["confidence_rationale"]:
        rationale = str((ind or {}).get("confidence_rationale") or "").strip()
        if rationale:
            entry["confidence_rationale"] = rationale
    for code in (ind or {}).get("codes") or []:
        icd10 = str((code or {}).get("icd10_code") or "").strip()
        if not icd10 or icd10 in entry["_code_keys"]:
            continue
        entry["_code_keys"].add(icd10)
        entry["codes"].append({
            "icd10_code": icd10,
            "level": (code or {}).get("level"),
            "confidence": (code or {}).get("confidence"),
            "description": (code or {}).get("description"),
        })


def _dedup_normalized(names: Any, normalizer: Callable[[Any], str]) -> list[str]:
    """Deduped, first-seen list of `normalizer(name)` over `names` (NO regex)."""
    if isinstance(names, str):
        names = [names]
    keys: list[str] = []
    seen: set = set()
    for name in names or []:
        key = normalizer(name)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def normalized_brand_exact_keys(names: Any) -> list[str]:
    """The deduped list of normalize_brand values for a list of names (NO regex).

    Persisted at FDA build time as brand_exact_key -- the TIER-1 join key: case /
    whitespace / hyphen-AND normalization only, NO salt/form stripping, so a CMS
    brand matches the FDA brand_name exactly as written."""
    return _dedup_normalized(names, normalize_brand)


def normalized_brand_keys(names: Any) -> list[str]:
    """The deduped list of brand_join_key values for a list of names (NO regex).

    Persisted at FDA build time as brand_key (from openfda.brand_name, TIER 2) and
    generic_key (from openfda.generic_name, TIER 3) -- brand_join_key adds salt/form
    suffix stripping on top of normalize_brand, so the CMS->FDA join is an exact
    indexed equality on an already-normalized key (both sides normalized identically
    by brand_join_key)."""
    return _dedup_normalized(names, brand_join_key)


def aggregate_fda_records(fda_records: Iterable[dict]) -> dict | None:
    """Aggregate ONE brand's FDA label records (as returned by an indexed per-brand
    query) into a single brand entry, carrying the FULL FDA payload as FLAT keys.

    Input: the fdaProductToDiagnosisResolution records matched for one brand
    (already Rx-only -- OTC excluded upstream), each carrying openfda.brand_name /
    openfda.generic_name (lists), molecule (scalar), ingredient_rxcuis /
    ingredient_names (aligned lists), usage_summary (scalar), the openfda sub-fields,
    and indications (list of
    {indication, confidence_rationale, codes:[{icd10_code, level, confidence, description}]}).

    Returns one FLAT entry {brand, molecule, rxcuis, ingredient_names (aligned with
    rxcuis, both deduped by rxcui), usage_summary, the flat openfda lists
    (generic_name, substance_name, product_type, clinical_rxcui, product_ndc,
    application_number, spl_id, spl_set_id), indications (FULL objects unioned across
    the records, deduped by normalized text, codes[] unioned by icd10_code)}, or
    None when no records were supplied. `brand` is the canonical FDA brand_name,
    chosen deterministically as the first normalized openfda.brand_name by sort.
    """
    entry: dict[str, Any] = {
        "brand": None,
        "molecule": None,
        "rxcuis": [],
        "ingredient_names": [],
        "usage_summary": None,
        "indications": [],
    }
    for flat in _OPENFDA_FLAT_FIELDS.values():
        entry[flat] = []
    rx_seen: set = set()
    of_seen = {flat: set() for flat in _OPENFDA_FLAT_FIELDS.values()}
    ind_store: dict = {}
    ind_order: list = []
    brand_names: list[str] = []
    brand_seen: set = set()
    any_record = False
    for doc in fda_records:
        any_record = True
        openfda = doc.get("openfda") or {}
        for brand in openfda.get("brand_name") or []:
            nb = normalize_brand(brand)
            if nb and nb not in brand_seen:
                brand_seen.add(nb)
                brand_names.append(nb)
        molecule = str(doc.get("molecule") or "").strip() or None
        if entry["molecule"] is None and molecule:
            entry["molecule"] = molecule
        usage_summary = str(doc.get("usage_summary") or "").strip() or None
        if entry["usage_summary"] is None and usage_summary:
            entry["usage_summary"] = usage_summary
        # rxcui-set and its names, paired and deduped by rxcui so the two lists
        # stay aligned.
        ing_rxcuis = doc.get("ingredient_rxcuis") or []
        ing_names = doc.get("ingredient_names") or []
        for position, raw_rx in enumerate(ing_rxcuis):
            rx = str(raw_rx).strip()
            if not rx or rx in rx_seen:
                continue
            rx_seen.add(rx)
            entry["rxcuis"].append(rx)
            name = str(ing_names[position]).strip() if position < len(ing_names) else ""
            entry["ingredient_names"].append(name)
        for src_field, flat in _OPENFDA_FLAT_FIELDS.items():
            _extend_unique(entry[flat], of_seen[flat], openfda.get(src_field))
        for ind in doc.get("indications") or []:
            _merge_indication(ind_store, ind_order, ind)
    if not any_record:
        return None
    entry["brand"] = sorted(brand_names)[0] if brand_names else None
    entry["indications"] = [
        {k: v for k, v in ind_store[norm].items() if k != "_code_keys"}
        for norm in ind_order
    ]
    return entry


def compose_drugs_by_brand(
    raw_drugs: Iterable[dict], resolve: Callable[[Any], tuple]
) -> tuple[list[dict], list[dict]]:
    """Compose one provider's raw Part D drug rows into the v1 drug collection.

    CMS-driven and memory-bounded: for each raw row, `resolve(cms_brand)` returns
    the FDA payload (whose `brand` is the canonical brand) or None -- the caller
    supplies a resolver that queries the indexed FDA collection (brand key first,
    then generic key) and caches by distinct brand, so neither the FDA collection nor
    anything sized by the ~20MM CMS rows is held in memory. Rows are then grouped by
    the FINAL CANONICAL brand (the FDA brand_name adopted on either leg), SUMMING
    Tot_Clms / Tot_Day_Suply / Tot_Benes / Tot_Drug_Cst across every row that
    resolves to it -- so a branded fill and a generic fill of the same drug merge
    into one entry (no duplicate canonical brand).

    A row whose brand resolves to NEITHER leg is DISCARDED (the composed drug
    collection holds only drugs mappable to the FDA record); discards are grouped by
    the raw CMS brand key, summing their claims, for the caller to surface as a
    finding.

    Each kept entry carries the FULL FDA payload as FLAT keys (molecule, rxcui-set +
    ingredient names, usage_summary, the flat openfda lists, and the full indication
    objects with their ICD-10 codes), plus the CMS `generic`, the summed volumes, and
    `match_provenance` (which resolution tier matched: brand_name | brand_key |
    generic_name), carried from the resolver's payload.

    Returns (kept, discarded). Each kept entry conforms to the schema's drug-entry
    shape; each discarded entry carries {brand, brand_join_key, total_claims}.

    NOTE: the inner-join discard is correct ONLY against the COMPLETE FDA
    collection (a full-partition FDA rebuild); against a partial FDA build most
    brands would find no match and be discarded, so this step presumes the full FDA
    rebuild has run.
    """
    groups: dict[Any, dict] = {}
    group_order: list = []
    group_entry: dict[Any, dict] = {}
    discards: dict[str, dict] = {}
    discard_order: list = []
    for d in raw_drugs:
        fda_entry = resolve(d.get("brand"))
        canonical = fda_entry.get("brand") if fda_entry else None
        if fda_entry is None or not canonical:
            rk = brand_join_key(d.get("brand"))
            disc = discards.get(rk)
            if disc is None:
                disc = {
                    "brand": normalize_brand(d.get("brand")),
                    "brand_join_key": rk,
                    "total_claims": None,
                }
                discards[rk] = disc
                discard_order.append(rk)
            disc["total_claims"] = _sum_opt(disc["total_claims"], d.get("total_claims"))
            continue
        cur = groups.get(canonical)
        if cur is None:
            cur = {
                "brand": canonical,
                "generic": d.get("generic"),
                "total_claims": None,
                "days_supply": None,
                "beneficiary_count": None,
                "total_drug_cost": None,
            }
            groups[canonical] = cur
            group_order.append(canonical)
            group_entry[canonical] = fda_entry
        if not cur.get("generic") and d.get("generic"):
            cur["generic"] = d.get("generic")
        cur["total_claims"] = _sum_opt(cur["total_claims"], d.get("total_claims"))
        cur["days_supply"] = _sum_opt(cur["days_supply"], d.get("days_supply"))
        cur["beneficiary_count"] = _sum_opt(cur["beneficiary_count"], d.get("beneficiary_count"))
        cur["total_drug_cost"] = _sum_opt(cur["total_drug_cost"], d.get("total_drug_cost"))

    kept: list[dict] = []
    for canonical in group_order:
        cur = groups[canonical]
        fda = group_entry[canonical]
        cur["match_provenance"] = fda.get("match_provenance")
        cur["molecule"] = fda.get("molecule")
        cur["rxcuis"] = list(fda.get("rxcuis") or [])
        cur["ingredient_names"] = list(fda.get("ingredient_names") or [])
        cur["usage_summary"] = fda.get("usage_summary")
        for flat in _OPENFDA_FLAT_FIELDS.values():
            cur[flat] = list(fda.get(flat) or [])
        cur["indications"] = [
            {
                "indication": ind.get("indication"),
                "confidence_rationale": ind.get("confidence_rationale"),
                "codes": [dict(code) for code in ind.get("codes") or []],
            }
            for ind in fda.get("indications") or []
        ]
        kept.append(cur)
    discarded = [discards[rk] for rk in discard_order]
    return kept, discarded


_ROLLUP_USAGE = ("tot_clms", "tot_30day_fills", "tot_day_suply", "tot_drug_cst")


def rollup_specialty_flat(docs: Iterable[dict]) -> dict:
    """Roll flat provider x molecule documents up to the specialty (NUCC) grain
    (EPIC-010-F-007-S-009). A provider is attributed to EACH of its nucc_codes,
    so per-specialty totals overlap across specialties; molecule_real_total is
    the de-duplicated total per molecule (each provider counted once). Returns
    the per-(nucc, molecule) specialty aggregate, the global per-molecule real
    total, and the set of NUCC codes seen. Pure; the engine writes the documents
    and reads provider_count from Provider_v_5."""
    spec: dict[tuple[str, str], dict] = {}
    real: dict[str, dict] = {}
    nuccs: set[str] = set()

    def _bucket(store, key):
        b = store.get(key)
        if b is None:
            b = {**{k: 0 for k in _ROLLUP_USAGE}, "providers": set()}
            store[key] = b
        return b

    for d in docs:
        mid = d.get("molecule_id")
        npi = d.get("npi")
        if not mid:
            continue
        totals = d.get("totals") or {}
        r = _bucket(real, mid)
        if npi not in r["providers"]:
            r["providers"].add(npi)
            for k in _ROLLUP_USAGE:
                r[k] += (totals.get(k) or 0)
        for nucc in d.get("nucc_codes") or []:
            nuccs.add(nucc)
            s = _bucket(spec, (nucc, mid))
            s["providers"].add(npi)
            for k in _ROLLUP_USAGE:
                s[k] += (totals.get(k) or 0)
    return {"spec": spec, "real": real, "nuccs": nuccs}


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
