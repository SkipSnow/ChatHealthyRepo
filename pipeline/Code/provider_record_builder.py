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
        by_code: dict[str, dict] = {}
        for entry in taxonomies:
            code = (entry.get("code") or "").strip()
            if not code:
                continue
            existing = by_code.get(code)
            if existing is None or entry.get("primary"):
                by_code[code] = entry
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


def build_provider_record(
    raw: dict,
    *,
    npi: str,
    run_id: str | None = None,
    nucc_catalog: dict[str, dict] | None = None,
) -> dict:
    doc = normalize_raw_record(raw)
    doc["npi"] = str(npi).zfill(10)
    if run_id:
        doc["run_id"] = run_id
    etc = (raw.get("Entity Type Code") or raw.get("entity_type_code") or "").strip()
    if etc:
        doc["entity_type_code"] = etc
        doc["entity_type_code_label"] = "individual" if etc == "1" else "institutional"
    if nucc_catalog and doc.get("taxonomies"):
        for tax in doc["taxonomies"]:
            code = tax.get("code")
            entry = nucc_catalog.get(code or "")
            if entry:
                tax["classification"] = entry
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
