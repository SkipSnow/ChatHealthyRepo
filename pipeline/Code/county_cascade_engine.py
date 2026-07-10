# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""County enrichment per-record cascade — LLD §4.13."""

from __future__ import annotations

from county_source import CASCADE_ORDER, normalize_address_county
from pipeline_runtime import PipelineRuntime
from process_assignment_activity import (
    _pass1_zip,
    _pass2_census,
    _pass4_maps,
    _pass6_nppes,
)

PRACTICE_TYPES = {"primary_practice", "secondary_practice"}


def finalize_unresolved_counties(doc: dict) -> None:
    for addr in doc.get("addresses") or []:
        if not isinstance(addr, dict):
            continue
        if addr.get("address_type") not in PRACTICE_TYPES:
            continue
        county = addr.get("county")
        if not isinstance(county, dict) or not county.get("fips"):
            addr["county"] = {"fips": None, "name": "", "source": "county_unresolvable"}


def run_county_cascade(ctx, state: str, kind: str) -> dict:
    rt = PipelineRuntime(ctx)
    from county_enrichment_job import _get_crosswalk

    crosswalk = _get_crosswalk()
    filt = rt.partition_filter(state)
    processed = 0
    for doc in rt.providers_coll.find(filt):
        addrs = [
            a for a in (doc.get("addresses") or [])
            if isinstance(a, dict) and (a.get("address_type") in PRACTICE_TYPES)
        ]
        if kind == "secondary_practice":
            addrs = [a for a in addrs if a.get("address_type") == "secondary_practice"]
        elif kind == "primary_practice":
            addrs = [a for a in addrs if a.get("address_type") == "primary_practice"]
        if not addrs:
            continue
        work = dict(doc)
        work["addresses"] = addrs + [a for a in doc.get("addresses") or [] if a not in addrs]
        _pass1_zip(work, crosswalk)
        _pass2_census(work, None)
        _pass6_nppes(work, None)
        if ctx.args.google_maps_enabled:
            _pass4_maps(work, None)
        finalize_unresolved_counties(work)
        for addr in work.get("addresses") or []:
            if isinstance(addr, dict):
                normalize_address_county(addr)
        rt.providers_coll.replace_one({"_id": doc["_id"]}, work)
        processed += 1
    ctx.manifest.discrepancy_summary["county_cascade_order"] = list(CASCADE_ORDER)
    return {"state": state, "kind": kind, "processed": processed, "cascade_order": list(CASCADE_ORDER)}
