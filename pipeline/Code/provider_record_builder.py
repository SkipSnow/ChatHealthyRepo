# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Build normative provider records from raw NPPES staging rows."""

from __future__ import annotations

from address_dedup import dedupe_addresses
from normalize_provider_rows import normalize_raw_record
from record_subdoc_dedup import (
    dedupe_insurance,
    dedupe_licenses,
    merge_insurance,
    merge_licenses,
    normalize_license_shape,
)


def dedupe_within_record(doc: dict) -> dict:
    """Collapse duplicate licenses[] and insurance[] (and other arrays) on one NPI."""
    licenses = doc.get("licenses") or []
    if licenses:
        doc["licenses"] = dedupe_licenses(licenses)

    insurance = doc.get("insurance") or []
    if insurance:
        doc["insurance"] = dedupe_insurance(insurance)

    taxonomies = doc.get("taxonomies") or []
    if taxonomies:
        # NPPES can slot the same code twice (once with Primary=N and once
        # with Primary=Y). normalize_raw_record has already lifted the
        # primary designator to doc["primary_taxonomy_code"], so here we
        # just dedupe by code -- there is no per-entry primary flag to
        # preserve. First occurrence wins on entry contents.
        by_code: dict[str, dict] = {}
        for entry in taxonomies:
            code = (entry.get("code") or "").strip()
            if not code:
                continue
            by_code.setdefault(code, entry)
        doc["taxonomies"] = list(by_code.values())

    other_ids = doc.get("other_identifiers") or []
    if other_ids:
        seen_ids: set[str] = set()
        unique_ids: list[dict] = []
        for entry in other_ids:
            ident = (entry.get("identifier") or "").strip()
            if not ident or ident in seen_ids:
                continue
            seen_ids.add(ident)
            unique_ids.append(entry)
        doc["other_identifiers"] = unique_ids

    addresses = doc.get("addresses") or []
    if addresses:
        doc["addresses"] = dedupe_addresses(addresses)

    return doc


# v42 test-variance mutation table. TEMPORARY. Each entry names an NPI
# and a dotted field path with the value to overlay when the caller
# passes testing_variance=True. Used to inject test cases (e.g. the
# discrepancy-report end-to-end test overlays code DDDDDDDDDD onto a
# known real NPI so provider_flags_enrichment reaches the unresolved-
# taxonomy-code path deterministically). REMOVE these rows once the
# test each row supports has passed and is no longer needed.
_TEST_VARIANCE = [
    {"npi": "1669475695", "path": "primary_taxonomy_code", "value": "DDDDDDDDDD"},
    {"npi": "1669475695", "path": "taxonomies.0.code", "value": "DDDDDDDDDD"},
]


def _apply_test_variance(doc: dict) -> None:
    """Apply any _TEST_VARIANCE row whose npi matches doc['npi'].
    Mutates doc in place. Dotted paths supported for one-level nesting
    into list indices (e.g. 'taxonomies.0.code')."""
    npi = doc.get("npi")
    if not npi:
        return
    for row in _TEST_VARIANCE:
        if row.get("npi") != npi:
            continue
        path = row["path"].split(".")
        target = doc
        for i, key in enumerate(path):
            last = (i == len(path) - 1)
            if key.isdigit():
                idx = int(key)
                if not isinstance(target, list) or idx >= len(target):
                    break
                if last:
                    target[idx] = row["value"]
                else:
                    target = target[idx]
            else:
                if not isinstance(target, dict):
                    break
                if last:
                    target[key] = row["value"]
                else:
                    target = target.get(key)
                    if target is None:
                        break


def build_provider_record(
    raw: dict,
    *,
    npi: str,
    run_id: str | None = None,
    nucc_catalog: dict[str, dict] | None = None,
    testing_variance: bool = False,
) -> dict:
    doc = normalize_raw_record(raw)
    doc["npi"] = str(npi).zfill(10)
    if run_id:
        doc["run_id"] = run_id
    etc = (raw.get("Entity Type Code") or raw.get("entity_type_code") or "").strip()
    if etc:
        doc["entity_type_code"] = etc
        doc["entity_type_code_label"] = "individual" if etc == "1" else "institutional"
        # Type-1 only. True iff any Healthcare Provider Primary Taxonomy
        # Switch_N on the raw record is "Y" (normalize_raw_record lifts
        # such a match into doc["primary_taxonomy_code"]).
        if etc == "1":
            doc["is_primary_care"] = bool(doc.get("primary_taxonomy_code"))
    if nucc_catalog:
        # Per LLD v39 sec. 7.1: taxonomies[i].code_label MUST be populated
        # from the NUCC catalog "Display Name" column. Prior behavior
        # copied the ENTIRE catalog row into tax["classification"] on
        # every provider (~700 bytes duplicated per taxonomy per provider,
        # ~4GB across a national fire), was undocumented in the schema,
        # and buried the name inside classification.raw.Display_Name.
        # Deleted 2026-07-31. Only the Display Name goes on the record.
        for tax in doc.get("taxonomies") or []:
            code = (tax.get("code") or "").strip()
            entry = nucc_catalog.get(code)
            if entry:
                raw_row = entry.get("raw") or entry
                display_name = (raw_row.get("Display Name") or "").strip()
                if display_name:
                    tax["code_label"] = display_name
                # Per ChatHealthyProvidersSchema taxonomies items allOf:
                # when reference_status != current_reference, grouping /
                # classification / specialization / definition become
                # required. A code that resolved via nucc_catalog IS a
                # current-reference match; stamping this here keeps the
                # schema's conditional-required branch off the record for
                # the common case and preserves the ability to declare
                # non-current codes downstream (F-105 supplemented codes
                # get their reference_status stamped by normalize_nucc).
                tax.setdefault("reference_status", "current_reference")
    if testing_variance:
        _apply_test_variance(doc)
    return dedupe_within_record(doc)


def append_licenses_to_doc(doc: dict, new_licenses: list[dict]) -> dict:
    doc["licenses"] = merge_licenses(doc.get("licenses"), new_licenses)
    return doc


def append_insurance_to_doc(doc: dict, new_insurance: list[dict]) -> dict:
    merged = merge_insurance(doc.get("insurance"), new_insurance)
    if merged:
        doc["insurance"] = merged
    else:
        doc.pop("insurance", None)
    return doc
