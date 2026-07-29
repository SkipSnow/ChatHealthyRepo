# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Attach pl_pfile secondary practice addresses — LLD §4.9."""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


from address_dedup import address_location_key, dedupe_addresses, merge_address
from pipeline_runtime import PipelineRuntime

_log = ChatHealthyLoggingService()


def attach_practice_addresses(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    attached = 0
    skipped_dup = 0

    # pl_pfile CSV columns (from CMS NPPES practice-locations file) live
    # under row["raw"] per staging_loader._wrap_row. Column names carry
    # leading/trailing whitespace variance in the CMS file; the reads
    # below match the exact column names as published in pl_pfile*.csv.
    _PL_ADDR1 = "Provider Secondary Practice Location Address- Address Line 1"
    _PL_ADDR2 = "Provider Secondary Practice Location Address-  Address Line 2"
    _PL_CITY = "Provider Secondary Practice Location Address - City Name"
    _PL_STATE = "Provider Secondary Practice Location Address - State Name"
    _PL_ZIP = "Provider Secondary Practice Location Address - Postal Code"
    _PL_COUNTRY = "Provider Secondary Practice Location Address - Country Code (If outside U.S.)"
    _PL_PHONE = "Provider Secondary Practice Location Address - Telephone Number"

    for row in rt.staging_coll("pl_pfile").find({"run_id": rt.run_id}):
        npi = row.get("npi") or (row.get("raw") or {}).get("NPI")
        if not npi:
            continue
        raw = row.get("raw") or {}
        line1 = (raw.get(_PL_ADDR1) or "").strip()
        city = (raw.get(_PL_CITY) or "").strip()
        state = (raw.get(_PL_STATE) or "").strip()
        zip_code = (raw.get(_PL_ZIP) or "").strip()
        if not (line1 and city and state):
            continue

        incoming = {
            "line1": line1,
            "line2": (raw.get(_PL_ADDR2) or "").strip() or None,
            "city": city,
            "state": state,
            "zip": zip_code,
            "country": (raw.get(_PL_COUNTRY) or "US").strip() or "US",
            "phone": (raw.get(_PL_PHONE) or "").strip() or None,
            "address_type": "secondary_practice",
            "primary": False,
            "source": "pl_pfile",
            "county": {"fips": None},
        }
        loc_key = address_location_key(incoming)

        doc = rt.providers_coll.find_one({"npi": str(npi)}, {"addresses": 1})
        if not doc:
            continue

        addresses = list(doc.get("addresses") or [])
        existing_idx = next(
            (i for i, a in enumerate(addresses) if address_location_key(a) == loc_key),
            None,
        )
        if existing_idx is not None:
            addresses[existing_idx] = merge_address(addresses[existing_idx], incoming)
            skipped_dup += 1
        else:
            addresses.append(incoming)

        addresses = dedupe_addresses(addresses)
        result = rt.providers_coll.update_one(
            {"npi": str(npi)},
            {"$set": {"addresses": addresses}},
        )
        if result.modified_count:
            attached += 1

    return {"addresses_attached": attached, "skipped_duplicate_location": skipped_dup}
