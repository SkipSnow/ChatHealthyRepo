# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""County Enrichment Job — orchestrator and activity implementations.

Two-pass enrichment strategy:
  Pass 1: ZIP-based bulk update using ZipCountyCrosswalk.
          For ZIPs where res_ratio >= SPLIT_THRESHOLD (0.98), one updateMany
          per ZIP sets county_fips + county_name on all matching providers.
          ~220x fewer operations than per-record enrichment.

  Pass 2: Address-based enrichment for providers whose ZIP is split
          (res_ratio < 0.98) or not found in the crosswalk.
          Submits each provider's practice address to the US Census
          Geocoder API and issues one updateOne per provider.
"""

import logging
import os
import time
from datetime import datetime, timezone

try:
    import azure.durable_functions as df
except ImportError:
    df = None  # not available in local test environment

import requests
from pymongo import MongoClient, UpdateOne

_mongo: MongoClient | None = None


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(os.environ["MONGO_connectionString"])
    return _mongo


SPLIT_THRESHOLD = 0.98
CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/geographies/address"
CROSSWALK_COLLECTION = "PublicHealthData.ZipCountyCrosswalk"
PROVIDERS_COLLECTION = "PublicHealthData.providers_staging"


# ── Orchestrator ──────────────────────────────────────────────────────────────

def county_enrichment_orchestrator_fn(context):
    """County enrichment orchestrator. Runs Pass 1 then Pass 2."""
    config = context.get_input() or {}
    load_id = config.get("load_id", context.instance_id)
    config = {**config, "load_id": load_id}

    # Step 1: Get distinct ZIPs from providers_staging
    context.set_custom_status("Step 1/7: Getting distinct ZIPs from staging")
    zip_data = yield context.call_activity("get_distinct_zips_activity", config)
    total_providers = zip_data["total_providers"]
    distinct_zips = zip_data["distinct_zips"]

    # Step 2: Lookup ZIPs in crosswalk — split into confident vs ambiguous
    context.set_custom_status(f"Step 2/7: Looking up {len(distinct_zips):,} ZIPs in crosswalk")
    crosswalk_result = yield context.call_activity(
        "lookup_crosswalk_activity", {**config, "zips": distinct_zips}
    )
    confident_zips = crosswalk_result["confident"]   # ratio >= 0.98
    # ambiguous ZIPs (ratio < 0.98 or not found) are handled by Pass 2 via get_unenriched_fn

    # Step 3: Pass 1 — fan-out over confident ZIP batches
    num_workers = config.get("num_workers", 200)
    batch_size = max(1, len(confident_zips) // num_workers)
    zip_batches = [
        confident_zips[i:i + batch_size]
        for i in range(0, len(confident_zips), batch_size)
    ]
    context.set_custom_status(
        f"Step 3/7: Pass 1 — {len(confident_zips):,} confident ZIPs "
        f"across {len(zip_batches)} workers"
    )
    pass1_tasks = [
        context.call_activity("enrich_by_zip_batch_activity", {**config, "zip_batch": batch})
        for batch in zip_batches
    ]
    pass1_results = yield context.task_all(pass1_tasks)
    pass1_modified = sum(r.get("modified", 0) for r in pass1_results)

    # Step 4: Get providers not enriched by Pass 1
    context.set_custom_status("Step 4/7: Identifying unenriched providers for Pass 2")
    unenriched = yield context.call_activity("get_unenriched_activity", config)
    unenriched_count = unenriched["count"]
    unenriched_ids = unenriched["provider_ids"]

    # Step 5: Pass 2 — fan-out over unenriched provider batches
    addr_batch_size = config.get("addr_batch_size", 50)
    addr_batches = [
        unenriched_ids[i:i + addr_batch_size]
        for i in range(0, len(unenriched_ids), addr_batch_size)
    ]
    context.set_custom_status(
        f"Step 5/7: Pass 2 — {unenriched_count:,} providers via Census Geocoder "
        f"across {len(addr_batches)} workers"
    )
    pass2_tasks = [
        context.call_activity("enrich_by_address_batch_activity", {**config, "id_batch": batch})
        for batch in addr_batches
    ]
    pass2_results = yield context.task_all(pass2_tasks)
    pass2_modified = sum(r.get("modified", 0) for r in pass2_results)
    pass2_failed = sum(r.get("failed", 0) for r in pass2_results)

    # Step 6: Reconcile
    context.set_custom_status("Step 6/7: Reconciling enrichment counts")
    total_enriched = pass1_modified + pass2_modified
    still_unenriched = total_providers - total_enriched
    pass2_attempted = pass2_modified + pass2_failed
    reconcile = {
        "total_providers": total_providers,
        # Pass 1: ZIP-based bulk enrichment (res_ratio >= 0.98)
        "pass1_zip_enrichments": pass1_modified,
        # Pass 2: Address-based Census Geocoder enrichment (split ZIPs)
        "pass2_address_lookups_attempted": pass2_attempted,
        "pass2_address_lookups_succeeded": pass2_modified,
        "pass2_address_lookups_failed": pass2_failed,
        # Totals
        "total_enriched": total_enriched,
        "still_unenriched": still_unenriched,
        "match": still_unenriched == 0,
    }

    # Step 7: Report
    context.set_custom_status("Step 7/7: Writing enrichment report")
    yield context.call_activity("enrichment_report_activity", {**config, "reconcile": reconcile})

    status = "complete" if reconcile["match"] else "partial"
    context.set_custom_status(
        f"Done — {status}, {total_enriched:,}/{total_providers:,} enriched"
    )
    return reconcile


# ── Activity implementations ──────────────────────────────────────────────────

def get_distinct_zips_fn(config: dict) -> dict:
    """Return total provider count and list of distinct 5-digit ZIPs."""
    collection = config.get("staging_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    total = coll.count_documents({})
    pipeline = [
        {"$project": {"zip5": {"$substr": ["$practice_address.zip", 0, 5]}}},
        {"$group": {"_id": "$zip5"}},
    ]
    zips = [doc["_id"] for doc in coll.aggregate(pipeline) if doc["_id"]]
    logging.info("Found %d providers, %d distinct ZIPs", total, len(zips))
    return {"total_providers": total, "distinct_zips": zips}


def lookup_crosswalk_fn(config: dict) -> dict:
    """Look up each ZIP in ZipCountyCrosswalk. Split into confident vs ambiguous."""
    zips = config["zips"]
    xwalk_collection = config.get("crosswalk_collection", CROSSWALK_COLLECTION)
    db_name, coll_name = xwalk_collection.split(".", 1)
    docs = list(_get_mongo_client()[db_name][coll_name].find(
        {"zip": {"$in": zips}},
        {"zip": 1, "county_fips": 1, "county_name": 1, "res_ratio": 1, "is_split": 1}
    ))
    found_zips = {d["zip"] for d in docs}
    confident = [
        {"zip": d["zip"], "county_fips": d["county_fips"], "county_name": d["county_name"]}
        for d in docs if not d.get("is_split", True)
    ]
    ambiguous = [z for z in zips if z not in found_zips or
                 next((d for d in docs if d["zip"] == z and d.get("is_split")), None)]
    logging.info(
        "Crosswalk lookup: %d confident, %d ambiguous",
        len(confident), len(ambiguous)
    )
    return {"confident": confident, "ambiguous": ambiguous}


def enrich_by_zip_batch_fn(config: dict) -> dict:
    """Pass 1: updateMany for each ZIP in batch. One command per ZIP."""
    zip_batch = config["zip_batch"]  # list of {zip, county_fips, county_name}
    collection = config.get("staging_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    total_modified = 0
    for entry in zip_batch:
        zip5 = entry["zip"]
        result = coll.update_many(
            {
                "county_fips": {"$exists": False},
                "practice_address.zip": {"$regex": f"^{zip5}"},
            },
            {"$set": {
                "county_fips": entry["county_fips"],
                "county_name": entry["county_name"],
                "county_source": "crosswalk_pass1",
            }},
        )
        total_modified += result.modified_count
    return {"modified": total_modified}


def get_unenriched_fn(config: dict) -> dict:
    """Return _id list of providers without county_fips."""
    collection = config.get("staging_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    ids = [
        str(doc["_id"])
        for doc in coll.find(
            {"county_fips": {"$exists": False}},
            {"_id": 1}
        )
    ]
    logging.info("Unenriched providers for Pass 2: %d", len(ids))
    return {"count": len(ids), "provider_ids": ids}


def _geocode_address(street: str, city: str, state: str, zip_code: str) -> dict | None:
    """Call Census Geocoder. Returns {county_fips, county_name} or None."""
    try:
        params = {
            "street": street,
            "city": city,
            "state": state,
            "zip": zip_code[:5] if zip_code else "",
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "layers": "Counties",
            "format": "json",
        }
        resp = requests.get(CENSUS_GEOCODER_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        matches = data.get("result", {}).get("addressMatches", [])
        if not matches:
            return None
        geographies = matches[0].get("geographies", {})
        counties = geographies.get("Counties", [])
        if not counties:
            return None
        county = counties[0]
        state_fips = county.get("STATE", "")
        county_fips_suffix = county.get("COUNTY", "")
        return {
            "county_fips": state_fips + county_fips_suffix,
            "county_name": county.get("NAME", ""),
        }
    except Exception as exc:
        logging.warning("Census Geocoder failed for %s %s: %s", street, zip_code, exc)
        return None


def enrich_by_address_batch_fn(config: dict) -> dict:
    """Pass 2: Census Geocoder per provider. One updateOne per provider."""
    id_batch = config["id_batch"]  # list of _id strings
    collection = config.get("staging_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)

    from bson import ObjectId
    coll = _get_mongo_client()[db_name][coll_name]
    object_ids = [ObjectId(i) for i in id_batch]
    providers = list(coll.find(
        {"_id": {"$in": object_ids}},
        {"_id": 1, "practice_address": 1}
    ))

    ops = []
    modified = 0
    failed = 0

    for provider in providers:
        addr = provider.get("practice_address", {})
        result = _geocode_address(
            street=addr.get("line1", ""),
            city=addr.get("city", ""),
            state=addr.get("state", ""),
            zip_code=addr.get("zip", ""),
        )
        if result:
            ops.append(UpdateOne(
                {"_id": provider["_id"]},
                {"$set": {
                    "county_fips": result["county_fips"],
                    "county_name": result["county_name"],
                    "county_source": "geocoder_pass2",
                }}
            ))
            modified += 1
        else:
            failed += 1
        time.sleep(0.05)  # 20 req/sec — Census Geocoder rate limit

    if ops:
        coll.bulk_write(ops, ordered=False)

    return {"modified": modified, "failed": failed}


def enrichment_report_fn(config: dict) -> dict:
    """Write enrichment run report to admin.PipelineDiscrepancyReport."""
    reconcile = config["reconcile"]
    load_id = config.get("load_id", "unknown")
    report_collection = config.get("report_collection", "admin.PipelineDiscrepancyReport")
    db_name, coll_name = report_collection.split(".", 1)

    report = {
        "job": "CountyEnrichment",
        "load_id": load_id,
        "datetime": datetime.now(timezone.utc).isoformat(),
        "reconciliation": reconcile,
    }

    _get_mongo_client()[db_name][coll_name].insert_one(report)
    logging.info(
        "Enrichment report — load_id: %s | "
        "Pass 1 ZIP enrichments: %d | "
        "Pass 2 address lookups: %d attempted, %d succeeded, %d failed | "
        "Total enriched: %d/%d | Match: %s",
        load_id,
        reconcile.get("pass1_zip_enrichments", 0),
        reconcile.get("pass2_address_lookups_attempted", 0),
        reconcile.get("pass2_address_lookups_succeeded", 0),
        reconcile.get("pass2_address_lookups_failed", 0),
        reconcile.get("total_enriched", 0),
        reconcile.get("total_providers", 0),
        reconcile.get("match"),
    )
    return report


