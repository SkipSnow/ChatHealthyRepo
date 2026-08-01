# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Normalize NUCC staging: enrich raw NUCC rows with F-105 proprietary
data + insert F-105 supplement codes not in NUCC as new rows.

Runs in parallel with normalize_npi_per_state_fanout. Same prerequisites
(load_f006_catalog + load_staging_parallel). After this step completes,
PublicStaging.StagingNucc_v_{data_version} holds every code any provider
might carry (883 NUCC + N F-105 supplements). Each row is enriched with:

  - is_supplemented (bool)         true iff the code exists ONLY in F-105
  - can_prescribe (bool)           from F-105
  - is_homeopathic (bool)          from F-105
  - is_disqualified (bool)         from F-105 (BUG-005 curated allow/deny)

Realizes design directive 2026-08-01: 'normalization of NUCC ... add all
the metadata from the proprietary data and any missing records.'

Explicitly NOT in scope: populating dev_PublicHealthData.SpecialtyMetaData
on the front-end cluster. That is the Specialty Pipeline's job and runs
outside the Provider Pipeline.
"""
from __future__ import annotations

from pymongo import InsertOne, UpdateOne

from specialty_classification_catalog import load_catalog
from staging_loader import STAGING_DB_NAME, staging_collection_name


def execute(ctx) -> dict:
    mongo = ctx.mongo
    data_version = ctx.data_version
    run_id = ctx.run_id

    coll_name = staging_collection_name("nucc", data_version)
    coll = mongo[STAGING_DB_NAME][coll_name]

    f105 = load_catalog()  # {code -> {can_prescribe, is_homeopathic, is_supplemented, is_disqualified, grouping, classification, specialization, definition}}

    # Pass 1: enrich existing NUCC rows in place. Each row already carries
    # raw = {Code, Grouping, Classification, Specialization, Definition,
    # Notes, Display Name, Section} from the CSV load. Flatten those
    # NUCC fields to the top level (matches PublicHealthData.SpecialtyMetaData
    # production shape) and add the F-105 flags per LLD 8.4 / spec.
    update_ops: list[UpdateOne] = []
    staging_codes: set[str] = set()
    for row in coll.find({"run_id": run_id}, {"_id": 1, "raw": 1}):
        raw = row.get("raw") or {}
        code = raw.get("Code")
        if not code:
            continue
        staging_codes.add(code)
        f105_entry = f105.get(code, {})
        # For supplemented codes, F-105 is the authoritative source for
        # display_name and description peers (raw.* fields carry stale
        # values from prior normalize runs). For NUCC-authoritative codes
        # (non-supplement), raw.* is the authority.
        is_supp = bool(f105_entry.get("is_supplemented"))
        if is_supp:
            display_name = _derive_display_name(f105_entry)
            grouping = f105_entry.get("grouping") or raw.get("Grouping") or ""
            classification = f105_entry.get("classification") or raw.get("Classification") or ""
            specialization = f105_entry.get("specialization") or raw.get("Specialization") or ""
            definition = f105_entry.get("definition") or raw.get("Definition") or ""
        else:
            display_name = raw.get("Display Name") or ""
            grouping = raw.get("Grouping") or ""
            classification = raw.get("Classification") or ""
            specialization = raw.get("Specialization") or ""
            definition = raw.get("Definition") or ""
        set_doc = {
            "code": code,
            "Code": code,
            "Grouping": grouping,
            "Classification": classification,
            "Specialization": specialization,
            "Definition": definition,
            "Notes": raw.get("Notes") or "",
            "Display Name": display_name,
            "Section": raw.get("Section") or "",
            "is_supplemented": is_supp,
            "can_prescribe": bool(f105_entry.get("can_prescribe", False)),
            "is_homeopathic": bool(f105_entry.get("is_homeopathic", False)),
            "is_disqualified": bool(f105_entry.get("is_disqualified", False)),
        }
        if is_supp:
            # Also refresh raw.Display Name so subsequent re-normalize
            # runs read the authoritative label (not the stale prior).
            set_doc["raw.Display Name"] = display_name
        update_ops.append(UpdateOne({"_id": row["_id"]}, {"$set": set_doc}))
        if len(update_ops) >= 500:
            coll.bulk_write(update_ops, ordered=False)
            update_ops = []
    if update_ops:
        coll.bulk_write(update_ops, ordered=False)

    # Pass 2: insert F-105 supplement codes not present in NUCC staging.
    supplement_ops: list[InsertOne] = []
    for code, entry in f105.items():
        if not entry.get("is_supplemented"):
            continue
        if code in staging_codes:
            # Skip: supplement metadata already covers a code that IS in
            # NUCC (rare -- likely a data curation issue in F-105). We do
            # not overwrite the NUCC row; the supplement is redundant.
            continue
        supplement_ops.append(InsertOne({
            "run_id": run_id,
            "raw": {
                "Code": code,
                "Grouping": entry.get("grouping") or "",
                "Classification": entry.get("classification") or "",
                "Specialization": entry.get("specialization") or "",
                "Definition": entry.get("definition") or "",
                "Notes": entry.get("note") or "",
                "Display Name": _derive_display_name(entry),
                "Section": "Individual",
            },
            "code": code,
            # Production SMD-shape top-level metadata (matches Pass 1 shape)
            "Code": code,
            "Grouping": entry.get("grouping") or "",
            "Classification": entry.get("classification") or "",
            "Specialization": entry.get("specialization") or "",
            "Definition": entry.get("definition") or "",
            "Notes": entry.get("note") or "",
            "Display Name": _derive_display_name(entry),
            "Section": "Individual",
            # ChatHealthy F-105 proprietary flags
            "is_supplemented": True,
            "can_prescribe": bool(entry.get("can_prescribe", False)),
            "is_homeopathic": bool(entry.get("is_homeopathic", False)),
            "is_disqualified": bool(entry.get("is_disqualified", False)),
            "_source_row_index": -1,  # sentinel: not from source CSV
        }))
    supplements_inserted = 0
    if supplement_ops:
        res = coll.bulk_write(supplement_ops, ordered=False)
        supplements_inserted = int(res.inserted_count or 0)

    return {
        "nucc_rows_enriched": len(staging_codes),
        "supplements_inserted": supplements_inserted,
        "collection": coll_name,
    }


def _derive_display_name(entry: dict) -> str:
    """Return the NUCC-equivalent display label for an F-105 supplement
    entry. Authoritative source: entry['display_name'] (populated on
    every supplement entry per LLD 2026-08-01 -- 'Surgical' derived from
    specialization alone was misleading; the intended label for
    246ZS0400X is 'Surgical Technologist' matching NUCC 246ZS0410X).
    Falls back to specialization/classification/grouping ONLY if the
    supplement entry is missing display_name (schema violation)."""
    dn = (entry.get("display_name") or "").strip()
    if dn:
        return dn
    for key in ("specialization", "classification", "grouping"):
        v = (entry.get(key) or "").strip()
        if v:
            return v
    return ""
