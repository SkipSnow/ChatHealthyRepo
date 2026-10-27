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
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

from bson.decimal128 import Decimal128

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


def _num(val: Any) -> Decimal | int | None:
    """Parse a CMS numeric cell exactly: int when integral, else Decimal.
    None if blank.

    No float, ever. A binary float cannot hold a cent value -- 5994.01 is not
    representable -- and the error compounds over every addition in a fold of
    28M rows: it is what made tot_drug_cst read 13989119.740000082. Decimal
    holds the source's own digits, sums exactly, and is written to Mongo as
    Decimal128 by _bson (see that function for how the three layers line up).
    """
    if val is None or val == "":
        return None
    try:
        d = Decimal(str(val).replace(",", ""))
    except (TypeError, ValueError, InvalidOperation):
        return None
    return int(d) if d == d.to_integral_value() else d


def _dedup_names(names: Iterable[Any]) -> list[str]:
    """Names in first-seen order, blanks dropped, folded case-insensitively so
    OMEPRAZOLE and Omeprazole are one name."""
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        text = str(n or "").strip()
        if not text:
            continue
        key = " ".join(text.upper().split())
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _exact(val: Any) -> Decimal | None:
    """A measured value: always a Decimal, never an int.

    Money and fills are measured, not counted, so their type does not depend
    on the value: a cost of exactly 100 is 100 as a decimal, which keeps the
    contract's decimal type true for every record rather than for most of
    them.
    """
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val).replace(",", ""))
    except (TypeError, ValueError, InvalidOperation):
        return None


def _bson(val: Any) -> Any:
    """A parsed value as Mongo stores it: Decimal becomes Decimal128.

    The three layers hold the same number three ways and all of them are
    exact. JSON has no decimal type, so the published contract declares these
    fields `number` and says so in its note; Python carries them as Decimal;
    BSON carries them as Decimal128 (bsonType `decimal`). pymongo cannot
    encode a Decimal, so the conversion happens here, at the write boundary,
    rather than anywhere arithmetic is done.
    """
    if isinstance(val, Decimal):
        return Decimal128(val)
    if isinstance(val, dict):
        return {k: _bson(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_bson(v) for v in val]
    return val


def _dec(val: Any) -> Any:
    """The other side of _bson: a stored value brought back for arithmetic.

    Decimal128 carries no arithmetic of its own -- a + b on two of them is a
    TypeError -- so every value read from the store passes through here before
    it is summed or compared. Reading is as much a conversion as writing, and
    a rollup that reads its input is where the missing half would have bitten.
    """
    if isinstance(val, Decimal128):
        return val.to_decimal()
    if isinstance(val, dict):
        return {k: _dec(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_dec(v) for v in val]
    return val


def _npi(raw: dict, col: str) -> str:
    return str(raw.get(col) or "").strip()


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
        "tot_30day_fills": _exact(raw.get(C["total_30day_fills"])),
        "tot_day_suply": _num(raw.get(C["days_supply"])),
        "tot_drug_cst": _exact(raw.get(C["total_drug_cost"])),
        "tot_benes": None if bene_sup else _num(raw.get(C["beneficiary_count"])),
        "ge65_tot_clms": None if ge65_sup else _num(raw.get(C["ge65_total_claims"])),
        "ge65_tot_30day_fills": None if ge65_sup else _exact(raw.get(C["ge65_total_30day_fills"])),
        "ge65_tot_day_suply": None if ge65_sup else _num(raw.get(C["ge65_days_supply"])),
        "ge65_tot_drug_cst": None if ge65_sup else _exact(raw.get(C["ge65_total_drug_cost"])),
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




def _as_int(v: Any) -> Any:
    """A whole-valued Decimal published as an int; everything else unchanged.

    A count that folds to 3.00 is the number three and is published as 3. A
    money total is not made whole by this: 5994.01 stays a Decimal and reaches
    the store as Decimal128.
    """
    if isinstance(v, Decimal) and v == v.to_integral_value():
        return int(v)
    return v
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


# --- v83 molecule-grain FDA union (LLD v83 III.4.1) ---------------------------
# One record per molecule, assembling the molecule's per-label resolution
# results. Pure function: input is the label-result dicts the resolution step
# wrote to the transient collection (one per set_id), output is the published
# molecule document minus _id/provenance (the finalize step stamps those).
# Every label folds in exactly once by construction -- the caller groups by
# molecule_id, so set_ids is simply the group's labels.

_FDA_CLASS_ORDER = ("RX", "OTC", "UNDETERMINED")


def _stats_over(values: list) -> dict:
    """max/min/median/avg over numeric values (operator-pinned terms,
    2026-10-20): median = the middle value; avg = the sum divided by the
    count; MIN excludes zeros -- a 0 is not a min. All four null when no
    value qualifies.

    Each value keeps the type it arrived in. max and min are observed values
    and no arithmetic touches them, so a count of 59 is published as 59 and
    not 59.0; the median of an odd-length set is likewise an observed value.
    Only avg divides, and it divides in Decimal, so money and fills stay
    exact.
    """
    nums = [v for v in values if isinstance(v, (int, Decimal))
            and not isinstance(v, bool)]
    if not nums:
        return {"max": None, "min": None, "median": None, "avg": None}
    nonzero = [v for v in nums if v > 0]
    ordered = sorted(nums)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        median = ordered[middle]
    else:
        pair = ordered[middle - 1] + ordered[middle]
        median = pair // 2 if pair % 2 == 0 else _exact_mean([pair], 2)
    return {
        "max": max(nums),
        "min": min(nonzero) if nonzero else None,
        "median": median,
        "avg": _exact_mean(nums, len(nums)),
    }


def _exact_mean(values: list, count: int):
    """The mean as an exact number: an int when it divides evenly, otherwise a
    Decimal. Never a float."""
    total = sum(values)
    if isinstance(total, int) and total % count == 0:
        return total // count
    return Decimal(total) / Decimal(count)


def fda_molecule_union(label_results: list[dict]) -> dict:
    """Union one molecule's per-label resolution results into the molecule
    document body (LLD v83 III.4.1).

    Each label result carries: set_id, molecule (clean name string),
    molecule_source, openfda (raw sub-document), indications
    [{indication, confidence_rationale, codes:[{icd10_code, level,
    confidence, description}]}], usage_summary, resolution_confidence.

    Unions: set_ids (the contributing record keys); molecule.clean_names and
    _molecule_sources; molecule.rxcui/unii/substance_name from openfda;
    indications deduped by normalized text with ICD-10 codes unioned by code
    and split to level3/level4; drug_class as the SET of label classes;
    aggregated max/min/median/avg over the labels' resolution_confidence."""
    set_ids: list[str] = []
    # Each label's own product type, kept beside its set_id. The molecule's
    # drug_class is a union across every label it folds, so without this the
    # record cannot say whether a given product needed a prescription.
    label_classes: list[dict] = []
    clean_names: list[str] = []
    sources: list[str] = []
    rxcui: list[str] = []
    unii: list[str] = []
    substance: list[str] = []
    classes: list[str] = []
    brand_names: list = []
    generic_names: list = []
    usage_parts: list[str] = []
    seen = {k: set() for k in
            ("set_id", "clean", "source", "rxcui", "unii", "substance",
             "class", "usage")}
    ind_store: dict = {}
    ind_order: list = []

    for lr in label_results:
        sid = str(lr.get("set_id") or "").strip()
        if sid and sid not in seen["set_id"]:
            seen["set_id"].add(sid)
            set_ids.append(sid)
            product_type = ((lr.get("openfda") or {}).get("product_type")
                            or [None])[0]
            label_classes.append({
                "set_id": sid,
                "drug_class": str(lr.get("drug_class") or "").strip() or None,
                "product_type": product_type,
            })
        name = str(lr.get("molecule") or "").strip()
        # Folded case-insensitively: OMEPRAZOLE and Omeprazole are one name,
        # and the first spelling seen is the one kept.
        name_key = " ".join(name.upper().split())
        if name and name_key not in seen["clean"]:
            seen["clean"].add(name_key)
            clean_names.append(name)
        src = str(lr.get("molecule_source") or "").strip()
        if src and src not in seen["source"]:
            seen["source"].add(src)
            sources.append(src)
        openfda = lr.get("openfda") or {}
        for field, dest, key in (("rxcui", rxcui, "rxcui"),
                                 ("unii", unii, "unii"),
                                 ("substance_name", substance, "substance")):
            _extend_unique(dest, seen[key], openfda.get(field))
        brand_names.extend(openfda.get("brand_name") or [])
        generic_names.extend(openfda.get("generic_name") or [])
        cls = str(lr.get("drug_class") or "").strip().upper()
        if cls and cls not in seen["class"]:
            seen["class"].add(cls)
            classes.append(cls)
        usage = str(lr.get("usage_summary") or "").strip()
        if usage and usage not in seen["usage"]:
            seen["usage"].add(usage)
            usage_parts.append(usage)
        for ind in lr.get("indications") or []:
            _merge_indication(ind_store, ind_order, ind)

    indications = []
    for norm in ind_order:
        entry = ind_store[norm]
        level3 = sorted({c["icd10_code"] for c in entry["codes"]
                         if c.get("level") == 3})
        level4 = sorted({c["icd10_code"] for c in entry["codes"]
                         if c.get("level") == 4})
        code_confs = [_num(c.get("confidence")) for c in entry["codes"]
                      if isinstance(c.get("confidence"), (int, float, str))]
        code_confs = [c for c in code_confs if c is not None]
        indications.append({
            "raw_text": entry["indication"],
            "icd10": {"level3": level3, "level4": level4},
            # The crosswalk is the model's: inference. The trust carried is
            # the weakest code confidence in the union -- the crosswalk's own
            # floor -- or null when the union carries no confidence.
            "_crosswalk_provenance": "inference",
            "trust": min(code_confs) if code_confs else None,
        })

    doc = {
        "set_ids": set_ids,
        "label_classes": label_classes,
        "molecule": {
            "clean_names": clean_names,
            # The label's own brand and generic names: what the RxNorm
            # identity ladder resolves from, and what a reader checks a
            # resolution against.
            "label_brand_names": _dedup_names(brand_names),
            "label_generic_names": _dedup_names(generic_names),
            "rxcui": rxcui,
            "unii": unii,
            "substance_name": substance,
            "_molecule_sources": sources,
        },
        "indications": indications,
        "drug_class": {"classes": [c for c in _FDA_CLASS_ORDER if c in classes]},
        # The molecule's trust is the distribution of its own indications'
        # trust. The per-label resolution_confidence the resolver no longer
        # emits is why this block read null on every record.
        "trust": _stats_over([i["trust"] for i in indications
                              if i["trust"] is not None]),
    }
    if usage_parts:
        doc["usage"] = {"summarized_usage": "\n".join(usage_parts)}
    return doc


# --- v83 staging-sourced specialty rollup (LLD v83 III.4.3, III.10) -----------
# State-aggregated masters carry no per-NPI detail, so the NUCC rollup is
# computed from staging directly: accumulate per-(provider, molecule) totals,
# then roll to (nucc, molecule) with the operator-ruled statistics
# (max / min>0 / median / avg over the per-provider totals, R1 2026-10-20).

_ROLLUP_MEASURES = ("tot_clms", "tot_30day_fills", "tot_day_suply", "tot_drug_cst")

# Counted things: a whole-valued total is the integer it counts.
_COUNT_MEASURES = ("tot_clms", "tot_day_suply")
# Measured things: money and fills stay exact decimals even when whole, so a
# cost of exactly 100 is 100.00 and the contract's decimal type holds for
# every record rather than for most of them.
_EXACT_MEASURES = ("tot_30day_fills", "tot_drug_cst")


def _published(measure: str, value: Any) -> Any:
    """One rollup measure as it is published."""
    return _as_int(value) if measure in _COUNT_MEASURES else value


def accumulate_provider_molecule_totals(rows: Iterable[dict]) -> dict:
    """Phase 1: stream raw CMS Part D rows into per-(npi, molecule_id) all-ages
    totals. Rows without an NPI or a mintable molecule are skipped (the
    no-molecule bucket is measured by the pre-pass)."""
    C = PARTD_COLS
    acc: dict[tuple[str, str], dict] = {}
    for raw in rows:
        npi = _npi(raw, C["npi"])
        mid = molecule_id(raw.get(C["generic"]))
        if not npi or not mid:
            continue
        bucket = acc.get((npi, mid))
        if bucket is None:
            bucket = {k: (Decimal(0) if k in _EXACT_MEASURES else 0)
                      for k in _ROLLUP_MEASURES}
            acc[(npi, mid)] = bucket
        for field, col in (("tot_clms", C["total_claims"]),
                           ("tot_30day_fills", C["total_30day_fills"]),
                           ("tot_day_suply", C["days_supply"]),
                           ("tot_drug_cst", C["total_drug_cost"])):
            parse = _exact if field in _EXACT_MEASURES else _num
            val = parse(raw.get(col))
            if val is not None:
                bucket[field] += val
    return acc


def rollup_specialty_v83(acc: dict, nucc_by_npi: dict) -> dict:
    """Phase 2: roll the per-(npi, molecule) totals to the NUCC grain.

    Returns {nucc: {molecule_id: {specialty_total, specialty_stats,
    molecule_real_total, molecule_real_stats, prescriber_count}}}. A provider
    with multiple NUCC codes is attributed to EACH (overlapping specialty
    totals); the real side counts each provider once across specialties.
    Stats per measure: max / min (zeros excluded -- a 0 is not a min) /
    median (the middle value) / avg (sum over count)."""
    # real (de-duplicated) side: per molecule, the per-provider totals
    real_values: dict[str, dict[str, list]] = {}
    real_totals: dict[str, dict] = {}
    for (npi, mid), totals in acc.items():
        rv = real_values.setdefault(mid, {k: [] for k in _ROLLUP_MEASURES})
        rt_ = real_totals.setdefault(
            mid, {k: (Decimal(0) if k in _EXACT_MEASURES else 0)
                  for k in _ROLLUP_MEASURES})
        for k in _ROLLUP_MEASURES:
            rv[k].append(totals[k])
            rt_[k] += totals[k]
    real_stats = {mid: {k: _stats_over(vals[k]) for k in _ROLLUP_MEASURES}
                  for mid, vals in real_values.items()}
    real_counts = {mid: len(vals[_ROLLUP_MEASURES[0]])
                   for mid, vals in real_values.items()}

    # specialty-attributed side
    out: dict[str, dict[str, dict]] = {}
    spec_values: dict[tuple[str, str], dict[str, list]] = {}
    for (npi, mid), totals in acc.items():
        for nucc in nucc_by_npi.get(npi, []):
            sv = spec_values.setdefault((nucc, mid), {k: [] for k in _ROLLUP_MEASURES})
            for k in _ROLLUP_MEASURES:
                sv[k].append(totals[k])
    for (nucc, mid), vals in spec_values.items():
        entry = {
            "molecule_id": mid,
            "specialty_total": {k: _published(k, sum(vals[k]))
                                for k in _ROLLUP_MEASURES},
            "specialty_stats": {k: _stats_over(vals[k]) for k in _ROLLUP_MEASURES},
            "molecule_real_total": {k: _published(k, real_totals[mid][k])
                                    for k in _ROLLUP_MEASURES},
            "molecule_real_stats": real_stats[mid],
            "prescriber_count": len(vals[_ROLLUP_MEASURES[0]]),
        }
        out.setdefault(nucc, {})[mid] = entry
    return out
