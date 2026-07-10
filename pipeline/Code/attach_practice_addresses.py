# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Attach pl_pfile secondary practice addresses — LLD §4.9."""

from __future__ import annotations

import logging

from address_dedup import address_location_key, dedupe_addresses, merge_address
from pipeline_runtime import PipelineRuntime

_log = logging.getLogger("attach_practice_addresses")


def attach_practice_addresses(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    attached = 0
    skipped_dup = 0

    for row in rt.staging_coll("pl_pfile").find({"run_id": rt.run_id}):
        npi = row.get("npi")
        if not npi:
            continue
        addr = row.get("address")
        if not addr:
            continue

        incoming = {
            **addr,
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
