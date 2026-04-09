# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""County Enrichment Job — orchestrator and activity implementations.

Enrichment strategy (six passes):
  Pass 1: ZIP-based bulk update using ZipCountyCrosswalk.
          For ZIPs where res_ratio >= SPLIT_THRESHOLD (0.98), one updateMany
          per ZIP sets county_fips + county_name on all matching providers.
          ~220x fewer operations than per-record enrichment.

  Pass 2: Census Geocoder batch API (practice address) for providers whose
          ZIP is split (res_ratio < 0.98) or not found in the crosswalk.
          Sends up to 500 addresses per batch POST. On error, providers are
          marked geocoder_failed for retry in later passes.

  Pass 3: Census Geocoder batch API (billing/mailing address) for providers
          marked geocoder_failed after Pass 2. Uses mailing_address instead
          of practice_address.

  Pass 4: Google Maps Geocoding API (practice address) for providers still
          geocoder_failed after Pass 3. Paid API — requires google_maps_enabled=True.

  Pass 5: Google Maps Geocoding API (billing address) for providers still
          geocoder_failed after Pass 4. Paid API — requires google_maps_enabled=True.

  Pass 6: NPPES public registry lookup by NPI for providers still geocoder_failed
          after Pass 5. Free, no API key required. ~5 req/s rate limit per IP.
          Fetches canonical practice address from CMS registry, then tries ZIP
          crosswalk. Optional states_filter limits scope for test runs.
"""

import csv
import io
import logging
import math
import os
import time
from datetime import datetime, timezone


try:
    import azure.durable_functions as df
except ImportError:
    df = None  # not available in local test environment

import requests
from bson import ObjectId
from pipeline_worker_base import PipelineWorkerBase
from pymongo import MongoClient, UpdateOne

_mongo: MongoClient | None = None
_crosswalk: dict | None = None  # zip → {fips, name, ratio, is_split}
_maps_county_lookup: dict | None = None  # (state_fips_2d, county_name_lower) → 5-digit fips


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(os.environ["MONGO_connectionString"])
    return _mongo


def _get_crosswalk() -> dict:
    global _crosswalk
    if _crosswalk is None:
        coll = _get_mongo_client()[f"{_ENV_PREFIX}_PublicHealthData"]["ZipCountyCrosswalk"]
        _crosswalk = {
            d["zip"]: {
                "fips": d["county_fips"],
                "name": d["county_name"],
                "ratio": d.get("res_ratio"),
                "is_split": d.get("is_split", False),
            }
            for d in coll.find(
                {},
                {"zip": 1, "county_fips": 1, "county_name": 1, "res_ratio": 1, "is_split": 1}
            )
        }
        logging.info("Crosswalk cache loaded: %d ZIPs", len(_crosswalk))
    return _crosswalk


_fips_to_name: dict[str, str] | None = None

def _get_fips_to_name() -> dict[str, str]:
    """FIPS→county name reverse index built from the crosswalk cache."""
    global _fips_to_name
    if _fips_to_name is None:
        _fips_to_name = {v["fips"]: v["name"] for v in _get_crosswalk().values() if v.get("fips") and v.get("name")}
    return _fips_to_name


def _get_maps_county_lookup() -> dict:
    """Build (state_fips_2d, county_name_lower) → 5-digit county_fips from the crosswalk.

    Used by Pass 4 to resolve county names returned by Google Maps into FIPS codes.
    Built lazily and cached per process lifetime.
    """
    global _maps_county_lookup
    if _maps_county_lookup is None:
        coll = _get_mongo_client()[f"{_ENV_PREFIX}_PublicHealthData"]["ZipCountyCrosswalk"]
        lookup: dict = {}
        for d in coll.find({}, {"county_fips": 1, "county_name": 1}):
            fips = d.get("county_fips", "")
            name = d.get("county_name", "")
            if fips and name and len(fips) == 5:
                key = (fips[:2], name.lower().strip())
                lookup.setdefault(key, fips)  # keep first on collision
        _maps_county_lookup = lookup
        logging.info("Maps county lookup built: %d entries", len(lookup))
    return _maps_county_lookup


def _geocode_single_maps(address: str, api_key: str) -> tuple[str | None, str | None]:
    """Call Google Maps Geocoding API for a single address.

    Returns (county_name, state_abbr) if resolved, (None, None) otherwise.
    Caller is responsible for rate limiting between calls.
    """
    try:
        resp = requests.get(
            GOOGLE_MAPS_GEOCODING_URL,
            params={"address": address, "key": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        if status == "ZERO_RESULTS":
            return None, None
        if status != "OK":
            logging.warning("Maps geocoder status '%s' for: %s", status, address)
            return None, None
        county = state = None
        for c in data["results"][0].get("address_components", []):
            types = c.get("types", [])
            if "administrative_area_level_2" in types:
                county = c["long_name"]
            elif "administrative_area_level_1" in types:
                state = c["short_name"]  # e.g. "CA"
        return county, state
    except Exception as exc:
        logging.warning("Maps geocoder error for '%s': %s", address, exc)
        return None, None


SPLIT_THRESHOLD = 0.98
CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
GOOGLE_MAPS_GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_ENV_PREFIX = os.environ.get("ENV_PREFIX", "dev")
CROSSWALK_COLLECTION = f"{_ENV_PREFIX}_PublicHealthData.ZipCountyCrosswalk"
PROVIDERS_COLLECTION = f"{_ENV_PREFIX}_PublicHealthData.providers"

# US state/territory abbreviation → 2-digit FIPS (used by Pass 4 to resolve Maps results)
_STATE_ABBR_TO_FIPS: dict[str, str] = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06",
    "CO": "08", "CT": "09", "DE": "10", "FL": "12", "GA": "13",
    "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19",
    "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24",
    "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29",
    "MT": "30", "NE": "31", "NV": "32", "NH": "33", "NJ": "34",
    "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45",
    "SD": "46", "TN": "47", "TX": "48", "UT": "49", "VT": "50",
    "VA": "51", "WA": "53", "WV": "54", "WI": "55", "WY": "56",
    "DC": "11", "PR": "72", "VI": "78", "GU": "66", "AS": "60", "MP": "69",
}


# ── Orchestrators ─────────────────────────────────────────────────────────────

def _build_enrichment_reconcile(
    pass1_result: dict,
    pass2_result: dict,
    pass3_result: dict | None = None,
    pass4_result: dict | None = None,
    pass6_result: dict | None = None,
) -> dict:
    """Build combined reconcile dict from Pass 1–6 results.

    pass1_result may be empty ({}) when start_step > 3 skips Pass 1.
    still_unenriched and match are computed against addressable providers
    (total minus out_of_scope) so inactive/foreign providers do not prevent
    a complete match.
    """
    pass3_result = pass3_result or {}
    pass4_result = pass4_result or {}
    pass6_result = pass6_result or {}
    total_providers = (
        pass1_result.get("total_providers")
        or pass2_result.get("total_providers", 0)
    )
    out_of_scope           = pass1_result.get("out_of_scope", 0)
    addressable            = total_providers - out_of_scope
    pass1_modified         = pass1_result.get("pass1_modified", 0)
    pass2_modified         = pass2_result.get("pass2_modified", 0)
    pass2_billing_modified = pass2_result.get("pass2_billing_modified", 0)
    pass2_geocoder_failed  = pass2_result.get("pass2_geocoder_failed", pass2_result.get("pass2_failed", 0))
    pass2_no_address       = pass2_result.get("pass2_no_address", 0)
    pass2_failed           = pass2_geocoder_failed + pass2_no_address
    pass3_modified         = pass3_result.get("pass3_modified", 0)
    pass4_modified         = pass4_result.get("pass4_modified", 0)
    pass6_modified         = pass6_result.get("pass6_modified", 0)
    total_enriched         = pass1_modified + pass2_modified + pass2_billing_modified + pass3_modified + pass4_modified + pass6_modified
    # still_unenriched = addressable providers not yet enriched and not permanently failed
    still_unenriched       = addressable - total_enriched - pass2_failed
    return {
        "total_providers":                total_providers,
        "out_of_scope":                   out_of_scope,
        "addressable":                    addressable,
        "pass1_zip_enrichments":          pass1_modified,
        "pass1_batch_results":            pass1_result.get("pass1_batch_results", []),
        "pass2_address_lookups_attempted": pass2_modified + pass2_billing_modified + pass2_failed,
        "pass2_practice_enrichments":     pass2_modified,
        "pass2_billing_enrichments":      pass2_billing_modified,
        "pass2_geocoder_failed":          pass2_geocoder_failed,
        "pass2_no_address":               pass2_no_address,
        "pass2_address_lookups_failed":   pass2_failed,
        "pass2_batch_results":            pass2_result.get("pass2_batch_results", []),
        "pass3_billing_enrichments":      pass3_modified,
        "pass3_batch_results":            pass3_result.get("pass3_batch_results", []),
        "pass4_maps_enrichments":         pass4_modified,
        "pass4_batch_results":            pass4_result.get("pass4_batch_results", []),
        "pass6_nppes_enrichments":        pass6_modified,
        "pass6_batch_results":            pass6_result.get("pass6_batch_results", []),
        "total_enriched":                 total_enriched,
        "still_unenriched":               still_unenriched,
        "match":                          still_unenriched == 0,
    }


def county_enrichment_pass1_orchestrator_fn(context):
    """Pass 1: ZIP-based bulk enrichment via ZipCountyCrosswalk.

    Ensures the county.fips index, marks inactive/foreign providers as
    out_of_scope, then fans out one updateMany per confident ZIP.
    Returns results for the caller to combine with Pass 2.
    """
    config = context.get_input() or {}
    load_id = config.get("load_id", context.instance_id)
    config = {**config, "load_id": load_id}

    # Step 1: Ensure county.fips index. Idempotent — no-op if already exists.
    # Required when Pass 1 is run outside FullProviderPipeline.
    context.set_custom_status("Step 1/6: Ensuring county.fips index")
    yield context.call_activity("ensure_postload_indexes_activity", config)

    # Step 2: Mark inactive, foreign, and deactivated providers as out_of_scope.
    # Each condition gets its own reason sub-field for reporting and runtime resolution.
    context.set_custom_status("Step 2/6: Marking inactive/foreign/deactivated providers as out_of_scope")
    out_of_scope_result = yield context.call_activity("mark_out_of_scope_activity", config)

    # Step 3: Mark ZIP/state mismatches as out_of_scope (bad source data from NPPES).
    context.set_custom_status("Step 3/6: Marking ZIP/state mismatch providers as out_of_scope")
    zip_mismatch_result = yield context.call_activity("mark_zip_state_mismatch_activity", config)

    out_of_scope_count = (
        out_of_scope_result.get("marked_out_of_scope", 0)
        + zip_mismatch_result.get("marked_zip_state_mismatch", 0)
    )

    # Step 4: Get distinct ZIPs
    context.set_custom_status("Step 4/6: Getting distinct ZIPs from staging")
    zip_data = yield context.call_activity("get_distinct_zips_activity", config)
    total_providers = zip_data["total_providers"]
    distinct_zips = zip_data["distinct_zips"]

    # Step 5: Lookup crosswalk — split into confident vs ambiguous
    context.set_custom_status(f"Step 5/6: Looking up {len(distinct_zips):,} ZIPs in crosswalk")
    crosswalk_result = yield context.call_activity(
        "lookup_crosswalk_activity", {**config, "zips": distinct_zips}
    )
    confident_zips = crosswalk_result["confident"]

    # Step 6: Fan-out — one updateMany per ZIP (excludes out_of_scope providers)
    num_workers = config.get("num_workers", 200)
    batch_size = max(1, len(confident_zips) // num_workers)
    zip_batches = [
        confident_zips[i:i + batch_size]
        for i in range(0, len(confident_zips), batch_size)
    ]
    context.set_custom_status(
        f"Step 6/6: {len(confident_zips):,} confident ZIPs across {len(zip_batches)} workers"
    )
    pass1_tasks = [
        context.call_activity("enrich_by_zip_batch_activity", {**config, "zip_batch": batch})
        for batch in zip_batches
    ]
    pass1_results = (yield context.task_all(pass1_tasks)) if pass1_tasks else []
    pass1_modified = sum(r.get("modified", 0) for r in pass1_results)

    addressable = total_providers - out_of_scope_count
    context.set_custom_status(
        f"Done — {pass1_modified:,} enriched via ZIP crosswalk; "
        f"{out_of_scope_count:,} out_of_scope; {addressable:,} addressable"
    )
    return {
        "total_providers":   total_providers,
        "out_of_scope":      out_of_scope_count,
        "addressable":       addressable,
        "confident_zips":    len(confident_zips),
        "pass1_modified":    pass1_modified,
        "pass1_batch_results": pass1_results,
    }


def reset_geocoder_failed_fn(config: dict) -> dict:
    """Reset providers marked geocoder_failed so the batch geocoder can retry them.

    Previous runs using individual geocoder calls marked 499K providers as
    geocoder_failed due to rate limiting — not genuine address failures.
    This clears that flag so get_unenriched_fn picks them up again.
    Only call this when switching geocoder strategy; not on routine reruns.
    """
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    sf = _build_states_filter(config)  # BUG-PIPE-001
    result = _get_mongo_client()[db_name][coll_name].update_many(
        {"county.source": "geocoder_failed", "bad_data.flagged": {"$ne": True}, "out_of_scope.flagged": {"$ne": True}, **sf},
        {"$set": {"county": {"fips": None}}},
    )
    logging.info("Reset %d geocoder_failed records for retry", result.modified_count)
    return {"reset": result.modified_count}


def county_enrichment_pass2_orchestrator_fn(context):
    """Pass 2: Census Geocoder batch enrichment for providers with split or unknown ZIPs.

    Queries providers still missing county.fips, fans out Census Geocoder
    batch lookups, and returns results for the caller to combine with Pass 1.

    reset_failed (bool, default False): reset geocoder_failed records before
    querying so the batch geocoder can retry them. Use when switching from
    the old individual-call approach.
    """
    config = context.get_input() or {}
    load_id = config.get("load_id", context.instance_id)
    config = {**config, "load_id": load_id}

    # Optional: reset geocoder_failed records so batch geocoder can retry them
    if config.get("reset_failed", False):
        context.set_custom_status("Step 0/2: Resetting geocoder_failed records for retry")
        yield context.call_activity("reset_geocoder_failed_activity", config)

    # Step 1: Count unenriched providers and get _id range for partitioning
    context.set_custom_status("Step 1/2: Counting unenriched providers")
    unenriched = yield context.call_activity("get_unenriched_activity", config)
    unenriched_count = unenriched["count"]
    min_id = unenriched.get("min_id")
    max_id = unenriched.get("max_id")

    # Step 2: Fan-out — partition by _id range, each activity queries its own slice.
    # _id range avoids skip() scans: each activity uses {_id: {$gte: start, $lt: end}}
    # directly on the _id index. Works at any scale without connection pressure.
    # 500 addresses per Census batch: responds in seconds, not minutes.
    # 5,000-row CSVs consistently timed out (300s limit) on the free Census API.
    addr_batch_size = config.get("addr_batch_size", 500)
    num_batches = math.ceil(unenriched_count / addr_batch_size) if unenriched_count else 0
    context.set_custom_status(
        f"Step 2/2: {unenriched_count:,} providers via Census Geocoder "
        f"across {num_batches} workers"
    )
    # Compute evenly-spaced _id boundaries by interpolating the hex ObjectId space
    min_int = int(min_id, 16) if min_id else 0
    max_int = int(max_id, 16) if max_id else 0
    id_step = (max_int - min_int + 1) // num_batches if num_batches else 0
    pass2_tasks = [
        context.call_activity("enrich_by_address_batch_activity", {
            **config,
            "start_id": hex(min_int + id_step * i)[2:].zfill(24),
            "end_id":   hex(min_int + id_step * (i + 1))[2:].zfill(24) if i < num_batches - 1 else None,
        })
        for i in range(num_batches)
    ]
    pass2_results = (yield context.task_all(pass2_tasks)) if pass2_tasks else []
    pass2_modified         = sum(r.get("modified",          0) for r in pass2_results)
    pass2_billing_modified = sum(r.get("billing_modified",  0) for r in pass2_results)
    pass2_geocoder_failed  = sum(r.get("geocoder_failed",   0) for r in pass2_results)
    pass2_no_address       = sum(r.get("no_address",        0) for r in pass2_results)
    pass2_failed = pass2_geocoder_failed + pass2_no_address

    context.set_custom_status(
        f"Done — {pass2_modified:,} practice, {pass2_billing_modified:,} billing enriched; "
        f"{pass2_geocoder_failed:,} geocoder failed, {pass2_no_address:,} no address"
    )
    return {
        "unenriched_count": unenriched_count,
        "pass2_modified": pass2_modified,
        "pass2_billing_modified": pass2_billing_modified,
        "pass2_geocoder_failed": pass2_geocoder_failed,
        "pass2_no_address": pass2_no_address,
        "pass2_failed": pass2_failed,
        "pass2_batch_results": pass2_results,
    }


def county_enrichment_pass3_orchestrator_fn(context):
    """Pass 3: retry geocoder_failed providers using mailing/billing address.

    Providers that failed Pass 2 had practice addresses the Census geocoder
    couldn't match. Their mailing/billing address may be different and geocodable.
    On success sets county.source = geocoder_pass3_billing.
    On failure leaves county.source = geocoder_failed unchanged.
    """
    config = context.get_input() or {}
    load_id = config.get("load_id", context.instance_id)
    config = {**config, "load_id": load_id}

    context.set_custom_status("Step 1/2: Finding geocoder_failed providers with billing addresses")
    retryable = yield context.call_activity("get_billing_retryable_activity", config)
    retryable_count = retryable["count"]
    retryable_ids = retryable["provider_ids"]

    if not retryable_ids:
        context.set_custom_status("Done — no geocoder_failed providers with billing addresses")
        return {"pass3_retryable": 0, "pass3_modified": 0, "pass3_failed": 0, "pass3_batch_results": []}

    addr_batch_size = config.get("addr_batch_size", 5_000)
    addr_batches = [
        retryable_ids[i:i + addr_batch_size]
        for i in range(0, len(retryable_ids), addr_batch_size)
    ]
    context.set_custom_status(
        f"Step 2/2: {retryable_count:,} providers via billing address "
        f"across {len(addr_batches)} workers"
    )
    pass3_tasks = [
        context.call_activity("enrich_by_billing_batch_activity", {**config, "id_batch": batch})
        for batch in addr_batches
    ]
    pass3_results = (yield context.task_all(pass3_tasks)) if pass3_tasks else []
    pass3_modified = sum(r.get("modified", 0) for r in pass3_results)
    pass3_failed   = sum(r.get("geocoder_failed", 0) for r in pass3_results)

    context.set_custom_status(
        f"Done — {pass3_modified:,} billing enriched; {pass3_failed:,} still failed"
    )
    return {
        "pass3_retryable":     retryable_count,
        "pass3_modified":      pass3_modified,
        "pass3_failed":        pass3_failed,
        "pass3_batch_results": pass3_results,
    }


def county_enrichment_pass4_orchestrator_fn(context):
    """Pass 4: Google Maps Geocoding API as final fallback for geocoder_failed providers.

    Queries providers still geocoder_failed after Pass 3 and fans out Google Maps
    API calls to resolve county by address. Requires GOOGLE_MAPS_API_KEY in environment.
    Only runs when google_maps_enabled=True is passed in the pipeline config.
    """
    config = context.get_input() or {}
    load_id = config.get("load_id", context.instance_id)
    config = {**config, "load_id": load_id}

    context.set_custom_status("Step 1/2: Finding geocoder_failed providers for Maps retry")
    retryable = yield context.call_activity("get_maps_retryable_activity", config)
    retryable_count = retryable["count"]
    retryable_ids   = retryable["provider_ids"]

    if not retryable_ids:
        context.set_custom_status("Done — no geocoder_failed providers for Maps retry")
        return {"pass4_retryable": 0, "pass4_modified": 0, "pass4_failed": 0, "pass4_batch_results": []}

    maps_batch_size = config.get("maps_batch_size", 200)
    maps_batches = [
        retryable_ids[i:i + maps_batch_size]
        for i in range(0, len(retryable_ids), maps_batch_size)
    ]
    context.set_custom_status(
        f"Step 2/2: {retryable_count:,} providers via Google Maps "
        f"across {len(maps_batches)} workers"
    )
    pass4_tasks = [
        context.call_activity("enrich_by_maps_batch_activity", {**config, "id_batch": batch})
        for batch in maps_batches
    ]
    pass4_results = (yield context.task_all(pass4_tasks)) if pass4_tasks else []
    pass4_modified = sum(r.get("modified",    0) for r in pass4_results)
    pass4_failed   = sum(r.get("maps_failed", 0) for r in pass4_results)

    context.set_custom_status(
        f"Done — {pass4_modified:,} matched via Maps; {pass4_failed:,} still failed"
    )
    return {
        "pass4_retryable":     retryable_count,
        "pass4_modified":      pass4_modified,
        "pass4_failed":        pass4_failed,
        "pass4_batch_results": pass4_results,
    }


def county_enrichment_orchestrator_fn(context):
    """Standalone county enrichment: Pass 1 → Pass 2 → combined report.

    Used when CountyEnrichment is triggered directly via the Router.
    FullProviderPipeline calls Pass 1 and Pass 2 as separate visible steps instead.
    """
    config = context.get_input() or {}
    load_id = config.get("load_id", context.instance_id)
    config = {**config, "load_id": load_id}

    context.set_custom_status("Step 1/3: Pass 1 — ZIP bulk enrichment")
    pass1_result = yield context.call_sub_orchestrator(
        "county_enrichment_pass1_orchestrator", config
    )

    context.set_custom_status("Step 2/3: Pass 2 — Census Geocoder enrichment")
    pass2_result = yield context.call_sub_orchestrator(
        "county_enrichment_pass2_orchestrator", config
    )

    context.set_custom_status("Step 3/3: Writing enrichment report")
    reconcile = _build_enrichment_reconcile(pass1_result, pass2_result)
    yield context.call_activity("enrichment_report_activity", {**config, "reconcile": reconcile})

    status = "complete" if reconcile["match"] else "partial"
    context.set_custom_status(
        f"Done — {status}, {reconcile['total_enriched']:,}/{reconcile['total_providers']:,} enriched"
    )
    return reconcile


# ── Activity implementations ──────────────────────────────────────────────────

def get_distinct_zips_fn(config: dict) -> dict:
    """Return total provider count and list of distinct 5-digit ZIPs."""
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    state_filter = _build_states_filter(config)  # BUG-PIPE-001
    total = coll.count_documents(state_filter)
    pipeline = [
        {"$match": state_filter},  # BUG-PIPE-001
        {"$project": {"zip5": {"$substr": [
            {"$ifNull": [{"$toString": "$practice_address.zip"}, ""]},
            0, 5
        ]}}},
        {"$group": {"_id": "$zip5"}},
    ]
    zips = [doc["_id"] for doc in coll.aggregate(pipeline) if doc["_id"]]
    logging.info("Found %d providers, %d distinct ZIPs (states: %s)", total, len(zips), state_filter)
    return {"total_providers": total, "distinct_zips": zips}


def lookup_crosswalk_fn(config: dict) -> dict:
    """Look up each ZIP in the in-memory crosswalk cache. Split into confident vs ambiguous."""
    zips = config["zips"]
    xwalk = _get_crosswalk()
    confident = []
    ambiguous = []
    for zip_code in zips:
        entry = xwalk.get(zip_code)
        if entry and not entry["is_split"]:
            confident.append({
                "zip": zip_code,
                "county_fips": entry["fips"],
                "county_name": entry["name"],
                "res_ratio": entry["ratio"],
            })
        else:
            ambiguous.append(zip_code)
    logging.info("Crosswalk lookup: %d confident, %d ambiguous", len(confident), len(ambiguous))
    return {"confident": confident, "ambiguous": ambiguous}


class ZipEnrichmentWorker(PipelineWorkerBase):
    """Pass 1: ZIP-based bulk enrichment. One updateMany per ZIP entry."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.zip_batch = config["zip_batch"]
        self.provider_collection = config.get("provider_collection", PROVIDERS_COLLECTION)
        self._state_filter = _build_states_filter(config)  # BUG-PIPE-001: required
        self._idx: int = -1
        self._collection = None
        self._total_modified: int = 0
        self._started_at: str = ""
        self._start_time: float = 0.0

    def _pipeline_open(self) -> None:
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._start_time = time.monotonic()
        db_name, coll_name = self.provider_collection.split(".", 1)
        self._collection = _get_mongo_client()[db_name][coll_name]

    def _pipeline_has_next(self) -> bool:
        self._idx += 1
        return self._idx < len(self.zip_batch)

    def _pipeline_process(self) -> None:
        entry = self.zip_batch[self._idx]
        zip5 = entry["zip"]
        query = {
            "county.fips": None,
            "county.source": {"$ne": "out_of_scope"},
            "bad_data.flagged": {"$ne": True},
            "out_of_scope.flagged": {"$ne": True},
            "practice_address.zip": {"$regex": f"^{zip5}"},
            **self._state_filter,  # BUG-PIPE-001
        }
        result = self._collection.update_many(
            query,
            {"$set": {
                "county": {
                    "fips": entry["county_fips"],
                    "name": entry["county_name"],
                    "source": "crosswalk_pass1",
                    "zip_ratio": entry["res_ratio"],
                },
            }},
        )
        self._total_modified += result.modified_count

    def _pipeline_row_key(self) -> str:
        if 0 <= self._idx < len(self.zip_batch):
            return self.zip_batch[self._idx]["zip"]
        return f"zip_idx_{self._idx}"

    def _pipeline_resume(self) -> None:
        pass  # _pipeline_has_next() advances the cursor; no local state to reset

    def _pipeline_build_result(self) -> dict:
        return {
            "modified": self._total_modified,
            "started_at": self._started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(time.monotonic() - self._start_time, 2),
        }


def enrich_by_zip_batch_fn(config: dict) -> dict:
    """Pass 1: updateMany for each ZIP in batch. One command per ZIP."""
    return ZipEnrichmentWorker(config).pipeline_execute()


def mark_out_of_scope_fn(config: dict) -> dict:
    """Mark providers with data quality issues or out-of-scope status.

    PIPE-DQ-001 — bad_data flag (data quality issues, record retained but flagged):
    - no_address: both practice_address and mailing_address are absent.
      Sets bad_data: {flagged: true, reason: "no_address"}, county.fips: null.

    PIPE-DQ-002 — out_of_scope flag (valid record, outside processing scope):
    - foreign_provider: practice_address.country is set and is not "US".
      Sets out_of_scope: {flagged: true, reason: "foreign_provider"}, county.fips: null.
    - deactivated: npi_deactivation_date set without a later reactivation.
      Sets out_of_scope: {flagged: true, reason: "deactivated"}, county.fips: null.

    Uses controlled vocabulary values from CV-001 (bad_data_reasons) and CV-002 (out_of_scope_reasons).
    """
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    sf = _build_states_filter(config)  # BUG-PIPE-001
    _base = {
        "county.fips": None,
        "bad_data.flagged": {"$ne": True},
        "out_of_scope.flagged": {"$ne": True},
        "county.source": {"$nin": ["geocoder_failed", "geocoder_no_address", "out_of_scope"]},
        **sf,
    }

    # PIPE-DQ-001: no_address → bad_data flag
    r_no_address = coll.update_many(
        {**_base,
         "practice_address": {"$exists": False},
         "mailing_address":  {"$exists": False}},
        {"$set": {
            "bad_data": {"flagged": True, "reason": "no_address"},
            "county": {"fips": None},
        }},
    )
    logging.info("PIPE-DQ-001: flagged %d providers as bad_data (no_address)", r_no_address.modified_count)

    # PIPE-DQ-002: foreign_provider → out_of_scope flag
    r_foreign = coll.update_many(
        {**_base, "practice_address.country": {"$exists": True, "$ne": "US"}},
        {"$set": {
            "out_of_scope": {"flagged": True, "reason": "foreign_provider"},
            "county": {"fips": None},
        }},
    )
    logging.info("PIPE-DQ-002: flagged %d providers as out_of_scope (foreign_provider)", r_foreign.modified_count)

    # PIPE-DQ-002: deactivated → out_of_scope flag
    r_deactivated = coll.update_many(
        {**_base,
         "npi_deactivation_date":  {"$exists": True},
         "npi_reactivation_date":  {"$exists": False}},
        {"$set": {
            "out_of_scope": {"flagged": True, "reason": "deactivated"},
            "county": {"fips": None},
        }},
    )
    logging.info("PIPE-DQ-002: flagged %d providers as out_of_scope (deactivated)", r_deactivated.modified_count)

    total = r_no_address.modified_count + r_foreign.modified_count + r_deactivated.modified_count
    logging.info(
        "Marked %d providers — bad_data(no_address): %d, out_of_scope(foreign): %d, out_of_scope(deactivated): %d",
        total, r_no_address.modified_count, r_foreign.modified_count, r_deactivated.modified_count,
    )
    return {
        "marked_out_of_scope": total,
        "by_reason": {
            "no_address":       r_no_address.modified_count,
            "foreign_provider": r_foreign.modified_count,
            "deactivated":      r_deactivated.modified_count,
        },
    }


def mark_zip_state_mismatch_fn(config: dict) -> dict:
    """PIPE-DQ-003: Detect and repair zip/state mismatches using provider license data.

    Loads ZipCountyCrosswalk to build a ZIP → expected_state mapping.
    Providers where practice_address.zip maps to a different state than
    practice_address.state have bad source data from NPPES.

    Repair logic (checks provider licenses array):
    - If exactly one license state matches the zip's expected state → repair
      practice_address.state, clear bad_data flag (auto-repair).
    - If multiple license states match → bad_data (zip_state_mismatch_multiple_licenses).
    - If no license data → bad_data (zip_state_mismatch_no_license).
    - If license doesn't resolve → bad_data (zip_state_mismatch).

    Uses controlled vocabulary values from CV-001 (bad_data_reasons).
    Called after mark_out_of_scope_fn so already-excluded providers are skipped.
    """
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    # Build ZIP → state abbreviation from crosswalk + inverse of _STATE_ABBR_TO_FIPS
    crosswalk = _get_crosswalk()
    fips_to_abbrev = {v: k for k, v in _STATE_ABBR_TO_FIPS.items()}
    zip_to_state: dict[str, str] = {}
    for zip_code, data in crosswalk.items():
        fips = data.get("fips", "")
        if fips and len(fips) == 5:
            abbrev = fips_to_abbrev.get(fips[:2])
            if abbrev:
                zip_to_state[zip_code] = abbrev

    # Aggregate distinct (zip, state) pairs from unenriched, non-excluded providers
    sf = _build_states_filter(config)  # BUG-PIPE-001
    pipeline = [
        {"$match": {
            "county.fips": None,
            "out_of_scope.flagged": {"$ne": True},
            "bad_data.flagged": {"$ne": True},
            "county.source": {"$ne": "out_of_scope"},
            "practice_address.zip":   {"$exists": True},
            "practice_address.state": {"$exists": True},
            **sf,
        }},
        {"$group": {"_id": {
            "zip":   "$practice_address.zip",
            "state": "$practice_address.state",
        }}},
    ]

    # Build state → [mismatched ZIPs] index
    from collections import defaultdict
    mismatch_by_state: dict[str, list[str]] = defaultdict(list)
    for doc in coll.aggregate(pipeline, allowDiskUse=True):
        raw_zip = str(doc["_id"].get("zip") or "").strip()
        state   = str(doc["_id"].get("state") or "").strip().upper()
        zip5    = raw_zip[:5]
        if not zip5 or not state:
            continue
        expected = zip_to_state.get(zip5)
        if expected and expected != state:
            mismatch_by_state[state].append(zip5)

    # Process each mismatched provider individually to attempt license-based repair
    total_repaired = 0
    total_bad_data = 0
    ops: list = []

    for state, zip_list in mismatch_by_state.items():
        mismatched_providers = list(coll.find(
            {
                "county.fips": None,
                "out_of_scope.flagged": {"$ne": True},
                "bad_data.flagged": {"$ne": True},
                "county.source": {"$ne": "out_of_scope"},
                "practice_address.state": state,
                "practice_address.zip":   {"$in": zip_list},
                **sf,
            },
            {"_id": 1, "practice_address": 1, "licenses": 1},
        ))

        for p in mismatched_providers:
            zip5 = (p.get("practice_address", {}).get("zip") or "")[:5]
            expected_state = zip_to_state.get(zip5)
            if not expected_state:
                continue

            licenses = p.get("licenses") or []
            if not licenses:
                # PIPE-DQ-003: no license data — cannot repair
                ops.append(UpdateOne(
                    {"_id": p["_id"]},
                    {"$set": {
                        "bad_data": {"flagged": True, "reason": "zip_state_mismatch_no_license"},
                        "county": {"fips": None},
                    }},
                ))
                total_bad_data += 1
                continue

            # Extract unique license states
            license_states = set()
            for lic in licenses:
                ls = (lic.get("state") or "").strip().upper()
                if ls:
                    license_states.add(ls)

            matching_license_states = {ls for ls in license_states if ls == expected_state}

            if len(matching_license_states) == 1:
                # Exactly one license state matches the zip's expected state → repair
                ops.append(UpdateOne(
                    {"_id": p["_id"]},
                    {"$set": {
                        "practice_address.state": expected_state,
                        "bad_data": None,
                    }},
                ))
                total_repaired += 1
                logging.info(
                    "PIPE-DQ-003: repaired provider %s state %s→%s via license data",
                    p["_id"], state, expected_state,
                )
            elif len(license_states) > 1 and len(matching_license_states) >= 1:
                # Multiple license states — ambiguous
                ops.append(UpdateOne(
                    {"_id": p["_id"]},
                    {"$set": {
                        "bad_data": {"flagged": True, "reason": "zip_state_mismatch_multiple_licenses"},
                        "county": {"fips": None},
                    }},
                ))
                total_bad_data += 1
            else:
                # License data present but doesn't resolve the mismatch
                ops.append(UpdateOne(
                    {"_id": p["_id"]},
                    {"$set": {
                        "bad_data": {"flagged": True, "reason": "zip_state_mismatch"},
                        "county": {"fips": None},
                    }},
                ))
                total_bad_data += 1

        # Flush ops in batches
        if len(ops) >= 1000:
            coll.bulk_write(ops, ordered=False)
            ops = []

    if ops:
        coll.bulk_write(ops, ordered=False)

    logging.info(
        "PIPE-DQ-003: %d providers repaired via license, %d flagged as bad_data (zip_state_mismatch) across %d states",
        total_repaired, total_bad_data, len(mismatch_by_state),
    )
    return {
        "marked_zip_state_mismatch": total_bad_data,
        "repaired_via_license": total_repaired,
    }


# ── NUCC taxonomy prefixes with prescribing authority ──────────────────────
# These taxonomy code prefixes identify provider types that have independent
# or delegated prescribing authority in all or most US states.
_PRESCRIBER_TAX_PREFIXES = (
    "207",   # Allopathic & Osteopathic Physicians (all subspecialties)
    "208",   # Allopathic Physicians (continued)
    "209",   # Allopathic Physicians (continued)
    "204",   # Neuromusculoskeletal Medicine
    "174400000X",  # Optometrist
    "363L",  # Nurse Practitioner (all subtypes)
    "363A",  # Physician Assistant (all subtypes)
    "367",   # CRNA / Advanced Practice Midwife
    "122",   # Dentists (all subtypes)
    "213",   # Podiatrists
    "176",   # Certified Nurse Midwife
)


def mark_prescriber_fn(config: dict) -> dict:
    """PIPE-DQ-004: Classify providers by prescribing authority.

    Sets can_prescribe: {flagged: bool, method: "taxonomy", taxonomy_code: "..."}
    on every provider that does not already have the flag.

    Classification logic:
    - entity_type_code "2" (organizations) → can_prescribe = false
    - entity_type_code "1" (individuals) → check primary taxonomy code
      against _PRESCRIBER_TAX_PREFIXES. If primary is absent, check first
      taxonomy code.

    This flag is used by:
    - FindCare: to inform users whether a provider can prescribe
    - EvaluateCare: to determine if prescription behavior scoring applies

    Called after mark_out_of_scope_fn and mark_zip_state_mismatch_fn.
    """
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    sf = _build_states_filter(config)  # BUG-PIPE-001

    # Only process providers without the flag
    base_filter = {
        "can_prescribe": {"$exists": False},
        **sf,
    }

    total = coll.count_documents(base_filter)
    logging.info("PIPE-DQ-004: %d providers need can_prescribe classification", total)

    if total == 0:
        return {"classified": 0, "prescribers": 0, "non_prescribers": 0}

    ops = []
    prescribers = 0
    non_prescribers = 0

    cursor = coll.find(
        base_filter,
        {"npi": 1, "entity_type_code": 1, "taxonomies": 1, "_id": 1},
    )

    for doc in cursor:
        entity_type = doc.get("entity_type_code", "")

        if entity_type == "2":
            # Organizations cannot prescribe
            can = False
            tax_code = ""
            method = "organization"
        else:
            # Individual: check primary taxonomy, fall back to first
            taxonomies = doc.get("taxonomies", [])
            primary = next((t for t in taxonomies if t.get("primary")), None)
            tax_code = (primary or taxonomies[0] if taxonomies else {}).get("code", "")
            can = any(tax_code.startswith(p) for p in _PRESCRIBER_TAX_PREFIXES)
            method = "taxonomy"

        ops.append(UpdateOne(
            {"_id": doc["_id"]},
            {"$set": {
                "can_prescribe": {
                    "flagged": can,
                    "method": method,
                    "taxonomy_code": tax_code,
                },
            }},
        ))

        if can:
            prescribers += 1
        else:
            non_prescribers += 1

        if len(ops) >= 1000:
            coll.bulk_write(ops, ordered=False)
            ops = []

    if ops:
        coll.bulk_write(ops, ordered=False)

    logging.info(
        "PIPE-DQ-004: classified %d providers — prescribers: %d, non-prescribers: %d",
        prescribers + non_prescribers, prescribers, non_prescribers,
    )
    return {
        "classified": prescribers + non_prescribers,
        "prescribers": prescribers,
        "non_prescribers": non_prescribers,
    }


_UNENRICHED_FILTER = {
    "county.fips": None,
    "county.source": {"$nin": ["geocoder_failed", "geocoder_no_address", "out_of_scope"]},
    "bad_data.flagged": {"$ne": True},
    "out_of_scope.flagged": {"$ne": True},
}


def get_unenriched_fn(config: dict) -> dict:
    """Return count + _id range of unenriched providers, plus total provider count.

    Returns count and hex min/max _id so the orchestrator can partition the
    collection into _id ranges without loading or passing any ID lists.
    Each Pass 2 activity then queries its own range directly using the _id index.

    Excludes records already resolved or classified by a prior pass:
    geocoder_failed, geocoder_no_address, out_of_scope.
    mark_out_of_scope_fn must run before this.
    """
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    sf = _build_states_filter(config)  # BUG-PIPE-001
    enrichment_filter = {**_UNENRICHED_FILTER, **sf}
    total_providers = coll.count_documents(sf)
    unenriched = coll.count_documents(enrichment_filter)
    first = coll.find_one(enrichment_filter, {"_id": 1}, sort=[("_id", 1)])
    last  = coll.find_one(enrichment_filter, {"_id": 1}, sort=[("_id", -1)])
    min_id = str(first["_id"]) if first else None
    max_id = str(last["_id"])  if last  else None
    logging.info("Unenriched providers for Pass 2: %d / %d total (states: %s)", unenriched, total_providers, sf)
    return {"count": unenriched, "total_providers": total_providers, "min_id": min_id, "max_id": max_id}


def _geocode_batch(providers: list[dict]) -> dict:
    """Batch geocode providers via Census Geocoder batch API.

    Sends one CSV POST (up to 5K rows). Returns {str(_id): {"fips", "source"}}
    for matched providers only. Raises on HTTP/network errors so the caller
    can leave providers unenriched and retry on the next run.

    Response CSV columns (0-indexed):
      0  Unique ID   2  Match   8  State FIPS   9  County FIPS
    """
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
    id_to_billing: dict[str, bool] = {}

    for p in providers:
        practice = p.get("practice_address") or {}
        street   = practice.get("line1", "").strip()
        city     = practice.get("city",  "").strip()
        state    = practice.get("state", "").strip()
        zip_code = practice.get("zip",   "").strip()
        using_billing = False

        if not street and not city:
            mailing  = p.get("mailing_address") or {}
            street   = mailing.get("line1", "").strip()
            city     = mailing.get("city",  "").strip()
            state    = mailing.get("state", "").strip()
            zip_code = mailing.get("zip",   "").strip()
            using_billing = True

        pid = str(p["_id"])
        id_to_billing[pid] = using_billing
        writer.writerow([pid, street, city, state, zip_code[:5] if zip_code else ""])

    resp = requests.post(
        CENSUS_BATCH_URL,
        files={"addressFile": ("addresses.csv", buf.getvalue().encode("utf-8"), "text/csv")},
        data={"benchmark": "Public_AR_Current", "vintage": "Current_Current"},
        timeout=300,
    )
    resp.raise_for_status()

    results: dict[str, dict] = {}
    for row in csv.reader(io.StringIO(resp.text)):
        if len(row) < 10:
            continue
        pid, match, state_fp, county_fp = row[0].strip(), row[2].strip(), row[8].strip(), row[9].strip()
        if match not in ("Match", "Tie") or not state_fp or not county_fp:
            continue
        if pid in results:
            continue  # keep first match on Tie
        source = "geocoder_pass2_batch_billing" if id_to_billing.get(pid) else "geocoder_pass2_batch"
        results[pid] = {"fips": state_fp + county_fp, "source": source}

    logging.info("Census batch geocoder: %d/%d matched", len(results), len(providers))
    return results


def enrich_by_address_batch_fn(config: dict) -> dict:
    """Pass 2: Census Geocoder batch API — up to 5K providers per activity.

    Uses skip/limit with _id sort so no large ID list is passed through
    Durable Functions activity I/O (which has size limits).
    Each activity independently queries its own slice of unenriched providers.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()
    start_id_hex = config["start_id"]
    end_id_hex   = config.get("end_id")       # None for the last batch (open upper bound)
    collection   = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    sf = _build_states_filter(config)  # BUG-PIPE-001: defense-in-depth
    id_filter: dict = {"_id": {"$gte": ObjectId(start_id_hex)}}
    if end_id_hex:
        id_filter["_id"]["$lt"] = ObjectId(end_id_hex)

    providers = list(coll.find(
        {**_UNENRICHED_FILTER, **id_filter, **sf},
        {"_id": 1, "practice_address": 1, "mailing_address": 1, "licenses": 1},
    ))

    # Pre-screen: flag providers with no usable address
    geocodable: list[dict] = []
    ops: list = []
    no_address = 0
    for p in providers:
        practice = p.get("practice_address") or {}
        mailing  = p.get("mailing_address")  or {}
        if (practice.get("line1") or practice.get("city") or
                mailing.get("line1") or mailing.get("city")):
            geocodable.append(p)
        else:
            ops.append(UpdateOne(
                {"_id": p["_id"]},
                {"$set": {"county": {"fips": None, "source": "geocoder_no_address"}}},
            ))
            no_address += 1

    # Batch geocode — on error, leave providers unenriched for retry
    modified = billing_modified = geocoder_failed = 0
    if geocodable:
        batch_ok = False
        matched: dict = {}
        try:
            matched  = _geocode_batch(geocodable)
            batch_ok = True
        except Exception as exc:
            logging.error(
                "Census batch geocoder failed (%d providers left for retry): %s",
                len(geocodable), exc,
            )

        if batch_ok:
            for p in geocodable:
                pid = str(p["_id"])
                if pid in matched:
                    r = matched[pid]
                    ops.append(UpdateOne(
                        {"_id": p["_id"]},
                        {"$set": {"county": {
                            "fips": r["fips"],
                            "name": _get_fips_to_name().get(r["fips"], ""),
                            "source": r["source"],
                        }}},
                    ))
                    if "billing" in r["source"]:
                        billing_modified += 1
                    else:
                        modified += 1
                else:
                    # Batch succeeded but address had no match — permanent failure
                    ops.append(UpdateOne(
                        {"_id": p["_id"]},
                        {"$set": {"county": {"fips": None, "source": "geocoder_failed"}}},
                    ))
                    geocoder_failed += 1

    if ops:
        coll.bulk_write(ops, ordered=False)

    succeeded = modified + billing_modified
    return {
        "assigned":         len(providers),
        "succeeded":        succeeded,
        "modified":         modified,
        "billing_modified": billing_modified,
        "geocoder_failed":  geocoder_failed,
        "no_address":       no_address,
        "started_at":       started_at,
        "finished_at":      datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - start_time, 2),
    }


def get_billing_retryable_fn(config: dict) -> dict:
    """Return _id list of geocoder_failed providers that have a usable mailing/billing address.

    Excludes deactivated and foreign providers — same rules as Pass 2.
    """
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    sf = _build_states_filter(config)  # BUG-PIPE-001
    ids = [
        str(doc["_id"])
        for doc in coll.find(
            {
                "county.source": "geocoder_failed",
                "bad_data.flagged": {"$ne": True},
                "out_of_scope.flagged": {"$ne": True},
                **sf,
                "$and": [
                    {"$or": [
                        {"mailing_address.line1": {"$nin": [None, ""]}},
                        {"mailing_address.city":  {"$nin": [None, ""]}},
                    ]},
                    {"$or": [
                        {"npi_deactivation_date": {"$exists": False}},
                        {"npi_reactivation_date": {"$exists": True}},
                    ]},
                ],
                "$or": [
                    {"mailing_address.country": {"$exists": False}},
                    {"mailing_address.country": {"$in": [None, "", "US"]}},
                ],
            },
            {"_id": 1},
        )
    ]
    logging.info("Pass 3 billing-retryable providers: %d", len(ids))
    return {"count": len(ids), "provider_ids": ids}


def enrich_by_billing_batch_fn(config: dict) -> dict:
    """Pass 3: geocode geocoder_failed providers using mailing/billing address.

    Sends the mailing/billing address to the Census batch geocoder. On success,
    sets county.fips and source = geocoder_pass3_billing. On failure, leaves
    county.source = geocoder_failed unchanged (no write needed).
    """
    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()
    id_batch   = config["id_batch"]
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    object_ids = [ObjectId(i) for i in id_batch]
    providers  = list(coll.find(
        {"_id": {"$in": object_ids}},
        {"_id": 1, "mailing_address": 1},
    ))

    geocodable = [
        p for p in providers
        if (p.get("mailing_address") or {}).get("line1") or
           (p.get("mailing_address") or {}).get("city")
    ]

    modified = geocoder_failed = 0
    ops: list = []

    if geocodable:
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
        for p in geocodable:
            mailing  = p.get("mailing_address") or {}
            street   = mailing.get("line1", "").strip()
            city     = mailing.get("city",  "").strip()
            state    = mailing.get("state", "").strip()
            zip_code = mailing.get("zip",   "").strip()
            writer.writerow([str(p["_id"]), street, city, state, zip_code[:5] if zip_code else ""])

        batch_ok = False
        matched: dict = {}
        try:
            resp = requests.post(
                CENSUS_BATCH_URL,
                files={"addressFile": ("addresses.csv", buf.getvalue().encode("utf-8"), "text/csv")},
                data={"benchmark": "Public_AR_Current", "vintage": "Current_Current"},
                timeout=300,
            )
            resp.raise_for_status()
            for row in csv.reader(io.StringIO(resp.text)):
                if len(row) < 10:
                    continue
                pid, match, state_fp, county_fp = row[0].strip(), row[2].strip(), row[8].strip(), row[9].strip()
                if match not in ("Match", "Tie") or not state_fp or not county_fp:
                    continue
                if pid not in matched:
                    matched[pid] = state_fp + county_fp
            batch_ok = True
        except Exception as exc:
            logging.error(
                "Pass 3 billing batch geocoder failed (%d providers left for retry): %s",
                len(geocodable), exc,
            )

        if batch_ok:
            for p in geocodable:
                pid = str(p["_id"])
                if pid in matched:
                    ops.append(UpdateOne(
                        {"_id": p["_id"]},
                        {"$set": {"county": {
                            "fips": matched[pid],
                            "name": _get_fips_to_name().get(matched[pid], ""),
                            "source": "geocoder_pass3_billing",
                        }}},
                    ))
                    modified += 1
                else:
                    geocoder_failed += 1  # leave county.source = geocoder_failed unchanged

    if ops:
        coll.bulk_write(ops, ordered=False)

    logging.info("Pass 3 billing batch: %d matched, %d still failed", modified, geocoder_failed)
    return {
        "assigned":         len(providers),
        "succeeded":        modified,
        "modified":         modified,
        "geocoder_failed":  geocoder_failed,
        "started_at":       started_at,
        "finished_at":      datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - start_time, 2),
    }


def get_maps_retryable_fn(config: dict) -> dict:
    """Return _id list of geocoder_failed providers eligible for Google Maps retry.

    Any provider with a usable address string (practice or mailing) is eligible.
    """
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    sf = _build_states_filter(config)  # BUG-PIPE-001
    ids = [
        str(doc["_id"])
        for doc in coll.find(
            {
                "county.source": "geocoder_failed",
                "bad_data.flagged": {"$ne": True},
                "out_of_scope.flagged": {"$ne": True},
                **sf,
                "$or": [
                    {"practice_address.line1": {"$nin": [None, ""]}},
                    {"practice_address.city":  {"$nin": [None, ""]}},
                    {"mailing_address.line1":  {"$nin": [None, ""]}},
                    {"mailing_address.city":   {"$nin": [None, ""]}},
                ],
            },
            {"_id": 1},
        )
    ]
    logging.info("Pass 4 Maps-retryable providers: %d", len(ids))
    return {"count": len(ids), "provider_ids": ids}


def enrich_by_maps_batch_fn(config: dict) -> dict:
    """Pass 4: Google Maps Geocoding API for providers still geocoder_failed after Pass 3.

    Calls Maps API individually (no batch endpoint). Rate-limited by
    maps_call_delay_seconds (default 0.1s → ~10 calls/s per worker).
    With default 5 workers the pipeline runs at ~50 calls/s, at the Maps QPS limit.

    Resolves Maps (county_name, state_abbr) → FIPS via the ZipCountyCrosswalk lookup.
    On success: county.source = "geocoder_pass4_maps", county.fips set.
    On failure: county.source = "geocoder_failed" unchanged (no write).
    """
    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()
    id_batch   = config["id_batch"]
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    delay      = config.get("maps_call_delay_seconds", 0.1)
    api_key    = os.environ["GOOGLE_MAPS_API_KEY"]

    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    object_ids = [ObjectId(i) for i in id_batch]
    providers  = list(coll.find(
        {"_id": {"$in": object_ids}},
        {"_id": 1, "practice_address": 1, "mailing_address": 1},
    ))

    lookup = _get_maps_county_lookup()
    ops: list = []
    modified = maps_failed = 0

    for p in providers:
        practice = p.get("practice_address") or {}
        mailing  = p.get("mailing_address")  or {}

        addr_parts = [
            practice.get("line1") or mailing.get("line1", ""),
            practice.get("city")  or mailing.get("city",  ""),
            practice.get("state") or mailing.get("state", ""),
            (practice.get("zip")  or mailing.get("zip",   ""))[:5],
        ]
        address = ", ".join(part for part in addr_parts if part)

        if not address.strip():
            maps_failed += 1
            continue

        county_name, state_abbr = _geocode_single_maps(address, api_key)

        fips = None
        if county_name and state_abbr:
            state_fips = _STATE_ABBR_TO_FIPS.get(state_abbr.upper())
            if state_fips:
                fips = lookup.get((state_fips, county_name.lower().strip()))

        if fips:
            ops.append(UpdateOne(
                {"_id": p["_id"]},
                {"$set": {"county": {
                    "fips": fips,
                    "name": county_name or "",
                    "source": "geocoder_pass4_maps",
                }}},
            ))
            modified += 1
        else:
            maps_failed += 1

        if delay:
            time.sleep(delay)

    if ops:
        coll.bulk_write(ops, ordered=False)

    logging.info("Pass 4 Maps batch: %d matched, %d still failed", modified, maps_failed)
    return {
        "assigned":         len(providers),
        "succeeded":        modified,
        "modified":         modified,
        "maps_failed":      maps_failed,
        "started_at":       started_at,
        "finished_at":      datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - start_time, 2),
    }


def _build_states_filter(config: dict) -> dict:
    """Build a MongoDB query filter for state restriction. REQUIRED — raises if missing.

    BUG-PIPE-001: every enrichment step must filter by states. No default to all records.
    """
    states = config.get("states")
    if not states:
        raise ValueError("BUG-PIPE-001: states parameter is REQUIRED for enrichment. Cannot process all records.")
    if isinstance(states, list):
        return {"practice_address.state": {"$in": states}}
    if isinstance(states, dict):
        state_list = states.get("list", [])
        if not state_list:
            raise ValueError("BUG-PIPE-001: states.list is empty. Cannot process all records.")
        mode = states.get("mode", "include").lower()
        if mode == "include":
            return {"practice_address.state": {"$in": state_list}}
        if mode == "exclude":
            return {"practice_address.state": {"$nin": state_list}}
    raise ValueError(f"BUG-PIPE-001: invalid states format: {states}")




def get_nppes_retryable_fn(config: dict) -> dict:
    """Return _id + npi list of providers eligible for NPPES lookup.

    Targets providers where county.fips is still None (includes geocoder_failed
    and providers never processed by any geocoder pass).

    Optional states filter via config["states"]:
      {"mode": "include", "list": ["RI", "HI", "ME"]}  — only these states
      {"mode": "exclude", "list": ["CA", "TX"]}         — skip these states
      omitted                                            — all states
    """
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    query: dict = {
        "county.fips": None,
        "county.source": {"$ne": "out_of_scope"},
        "bad_data.flagged": {"$ne": True},
        "out_of_scope.flagged": {"$ne": True},
        "npi": {"$nin": [None, ""]},
        **_build_states_filter(config),  # BUG-PIPE-001: mandatory
    }

    providers = [
        {"id": str(doc["_id"]), "npi": doc["npi"]}
        for doc in coll.find(query, {"_id": 1, "npi": 1})
    ]
    logging.info("Pass 6 NPPES-retryable providers: %d", len(providers))
    return {"count": len(providers), "providers": providers}


def enrich_by_nppes_batch_fn(config: dict) -> dict:
    """Pass 6: NPPES public registry lookup for providers with no county.fips.

    Calls the free NPPES API (no key required) to fetch the canonical practice
    address registered with CMS for each NPI. On a new address, tries the ZIP
    crosswalk. Skips providers where NPPES returns the same address we already
    have (already failed geocoding, won't help to retry).

    Rate-limited by nppes_call_delay_seconds (default 0.2 s = 5 calls/s).
    Keep nppes_batch_size large (default 5000) so few activities fan out —
    all workers share the same outbound IP and hit the same rate limit.

    county.source values written:
      geocoder_pass6_nppes  — resolved via NPPES canonical address + crosswalk
    """
    NPPES_API = "https://npiregistry.cms.hhs.gov/api/"

    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()
    provider_batch = config["provider_batch"]   # list of {id, npi}
    collection     = config.get("provider_collection", PROVIDERS_COLLECTION)
    delay          = config.get("nppes_call_delay_seconds", 0.2)

    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    crosswalk = _get_crosswalk()

    object_ids = [ObjectId(p["id"]) for p in provider_batch]
    stored = {
        str(doc["_id"]): doc
        for doc in coll.find(
            {"_id": {"$in": object_ids}},
            {"_id": 1, "npi": 1, "practice_address": 1},
        )
    }

    ops: list = []
    modified = nppes_failed = nppes_not_found = nppes_same_address = 0

    for p in provider_batch:
        pid, npi = p["id"], p["npi"]
        doc = stored.get(pid)
        if not doc:
            nppes_failed += 1
            continue

        try:
            resp = requests.get(
                NPPES_API,
                params={"number": npi, "version": "2.1"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
        except Exception as exc:
            logging.warning("NPPES API error for NPI=%s: %s", npi, exc)
            nppes_failed += 1
            if delay:
                time.sleep(delay)
            continue

        if not results:
            nppes_not_found += 1
            if delay:
                time.sleep(delay)
            continue

        addrs = results[0].get("addresses", [])
        nppes_addr = next(
            (a for a in addrs if a.get("address_purpose") == "LOCATION"),
            addrs[0] if addrs else None,
        )

        if not nppes_addr:
            nppes_failed += 1
            if delay:
                time.sleep(delay)
            continue

        nppes_zip   = (nppes_addr.get("postal_code") or "")[:5].strip()
        nppes_state = (nppes_addr.get("state") or "").upper().strip()

        # If NPPES ZIP matches what we already have, geocoding it again won't help
        our_addr    = doc.get("practice_address") or {}
        our_zip     = (our_addr.get("zip") or "")[:5].strip()
        if nppes_zip and nppes_zip == our_zip:
            nppes_same_address += 1
            if delay:
                time.sleep(delay)
            continue

        # Try ZIP crosswalk on NPPES canonical ZIP
        fips = None
        cw = crosswalk.get(nppes_zip)
        if cw and not cw.get("is_split"):
            state_fips = _STATE_ABBR_TO_FIPS.get(nppes_state)
            if state_fips and cw["fips"][:2] == state_fips:
                fips = cw["fips"]

        if fips:
            ops.append(UpdateOne(
                {"_id": ObjectId(pid)},
                {"$set": {"county": {
                    "fips": fips,
                    "name": cw.get("name", ""),
                    "source": "geocoder_pass6_nppes",
                }}},
            ))
            modified += 1
        else:
            nppes_failed += 1

        if delay:
            time.sleep(delay)

    if ops:
        coll.bulk_write(ops, ordered=False)

    logging.info(
        "Pass 6 NPPES batch: %d matched, %d same address skipped, "
        "%d not in NPPES, %d failed",
        modified, nppes_same_address, nppes_not_found, nppes_failed,
    )
    return {
        "assigned":          len(provider_batch),
        "succeeded":         modified,
        "modified":          modified,
        "nppes_same_address": nppes_same_address,
        "nppes_not_found":   nppes_not_found,
        "nppes_failed":      nppes_failed,
        "started_at":        started_at,
        "finished_at":       datetime.now(timezone.utc).isoformat(),
        "duration_seconds":  round(time.monotonic() - start_time, 2),
    }


def county_enrichment_pass6_nppes_orchestrator_fn(context):
    """Pass 6: NPPES public registry lookup for remaining unenriched providers.

    Free API, no key required. Rate-limited to ~5 req/s per calling IP.
    Use states_filter to limit scope for test runs (e.g. ["RI", "HI", "ME"]).
    Use nppes_batch_size (default 5000) to control fan-out; keep it large to
    limit parallel workers sharing the same outbound IP.
    """
    config = context.get_input() or {}
    load_id = config.get("load_id", context.instance_id)
    config  = {**config, "load_id": load_id}

    states        = config.get("states")
    if states and isinstance(states, dict) and states.get("list"):
        mode         = states.get("mode", "include")
        states_label = f" ({mode}: {', '.join(states['list'])})"
    elif states and isinstance(states, list):
        states_label = f" (include: {', '.join(states)})"
    else:
        states_label = ""

    context.set_custom_status(f"Step 1/2: Finding NPPES-retryable providers{states_label}")
    retryable       = yield context.call_activity("get_nppes_retryable_activity", config)
    retryable_count = retryable["count"]
    providers       = retryable["providers"]

    if not providers:
        context.set_custom_status(f"Done — no NPPES-retryable providers{states_label}")
        return {
            "pass6_retryable": 0, "pass6_modified": 0,
            "pass6_failed": 0, "pass6_batch_results": [],
        }

    nppes_batch_size = config.get("nppes_batch_size", 5000)
    batches = [
        providers[i:i + nppes_batch_size]
        for i in range(0, len(providers), nppes_batch_size)
    ]
    context.set_custom_status(
        f"Step 2/2: {retryable_count:,} providers via NPPES{states_label} "
        f"across {len(batches)} workers"
    )
    pass6_tasks = [
        context.call_activity("enrich_by_nppes_batch_activity", {**config, "provider_batch": batch})
        for batch in batches
    ]
    pass6_results    = (yield context.task_all(pass6_tasks)) if pass6_tasks else []
    pass6_modified   = sum(r.get("modified",          0) for r in pass6_results)
    pass6_failed     = sum(r.get("nppes_failed",       0) for r in pass6_results)
    pass6_not_found  = sum(r.get("nppes_not_found",    0) for r in pass6_results)
    pass6_same_addr  = sum(r.get("nppes_same_address", 0) for r in pass6_results)

    context.set_custom_status(
        f"Done — {pass6_modified:,} enriched via NPPES; "
        f"{pass6_same_addr:,} same address skipped; "
        f"{pass6_not_found:,} not in NPPES; "
        f"{pass6_failed:,} failed{states_label}"
    )
    return {
        "pass6_retryable":     retryable_count,
        "pass6_modified":      pass6_modified,
        "pass6_failed":        pass6_failed,
        "pass6_not_found":     pass6_not_found,
        "pass6_same_address":  pass6_same_addr,
        "pass6_batch_results": pass6_results,
    }


def enrichment_report_fn(config: dict) -> dict:
    """Write enrichment run report to admin.PipelineDiscrepancyReports.

    Queries providers_staging by county.source to build a live summary with
    two percentages per bucket:
      pct_of_total       — share of all 8.8M NPI providers
      pct_of_addressable — share of active US providers (total minus out_of_scope)
    """
    reconcile = config["reconcile"]
    load_id = config.get("load_id", "unknown")
    report_collection = config.get("report_collection", "admin.PipelineDiscrepancyReports")
    provider_collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name_r, coll_name_r = report_collection.split(".", 1)
    db_name_s, coll_name_s = provider_collection.split(".", 1)

    client = _get_mongo_client()

    staging_coll = client[db_name_s][coll_name_s]
    sf = _build_states_filter(config)  # BUG-PIPE-001: mandatory state filter

    # Live count by county.source (null source = never touched)
    source_counts: dict = {
        doc["_id"]: doc["count"]
        for doc in staging_coll.aggregate([
            {"$match": sf},
            {"$group": {"_id": "$county.source", "count": {"$sum": 1}}}
        ])
    }

    # Out-of-scope breakdown by reason (None key → "legacy" for records without reason field)
    out_of_scope_by_reason: dict = {
        (doc["_id"] or "legacy"): doc["count"]
        for doc in staging_coll.aggregate([
            {"$match": {"county.source": "out_of_scope", **sf}},
            {"$group": {"_id": "$county.reason", "count": {"$sum": 1}}},
        ])
    }

    # PIPE-DQ-001/002: count records with new bad_data and out_of_scope flags
    bad_data_count = staging_coll.count_documents({"bad_data.flagged": True, **sf})
    out_of_scope_flagged_count = staging_coll.count_documents({"out_of_scope.flagged": True, **sf})

    # Bad data breakdown by reason
    bad_data_by_reason: dict = {
        (doc["_id"] or "unknown"): doc["count"]
        for doc in staging_coll.aggregate([
            {"$match": {"bad_data.flagged": True, **sf}},
            {"$group": {"_id": "$bad_data.reason", "count": {"$sum": 1}}},
        ])
    }

    # Out-of-scope (new flag) breakdown by reason
    out_of_scope_flagged_by_reason: dict = {
        (doc["_id"] or "unknown"): doc["count"]
        for doc in staging_coll.aggregate([
            {"$match": {"out_of_scope.flagged": True, **sf}},
            {"$group": {"_id": "$out_of_scope.reason", "count": {"$sum": 1}}},
        ])
    }

    total      = sum(source_counts.values())
    out_of_scope = source_counts.get("out_of_scope", 0)
    # Excluded = legacy out_of_scope + new bad_data + new out_of_scope flags
    total_excluded = out_of_scope + bad_data_count + out_of_scope_flagged_count
    addressable  = total - total_excluded  # providers the geocoder can reach

    def pct(n: int, d: int) -> float:
        return round(n / d * 100, 1) if d else 0.0

    def bucket(keys: list[str | None]) -> dict:
        count = sum(source_counts.get(k, 0) for k in keys)
        return {
            "count":            count,
            "pct_of_total":     pct(count, total),
            "pct_of_addressable": pct(count, addressable),
        }

    _enriched_sources = [
        "crosswalk_pass1",
        "geocoder_pass2", "geocoder_pass2_billing",
        "geocoder_pass2_batch", "geocoder_pass2_batch_billing",
        "geocoder_pass3_billing",
        "geocoder_pass4_maps",
        "geocoder_pass6_nppes",
    ]
    _enriched_total = sum(source_counts.get(s, 0) for s in _enriched_sources)

    summary = {
        "pass1_zip":        bucket(["crosswalk_pass1"]),
        "pass2_individual": bucket(["geocoder_pass2", "geocoder_pass2_billing"]),
        "pass2_batch":      bucket(["geocoder_pass2_batch", "geocoder_pass2_batch_billing"]),
        "pass3_billing":    bucket(["geocoder_pass3_billing"]),
        "pass4_maps":       bucket(["geocoder_pass4_maps"]),
        "pass6_nppes":      bucket(["geocoder_pass6_nppes"]),
        "geocoder_failed":  bucket(["geocoder_failed"]),
        "no_address":       bucket(["geocoder_no_address"]),
        "out_of_scope":     {   # pct_of_addressable is N/A — these ARE the excluded set
            "count":              out_of_scope,
            "pct_of_total":       pct(out_of_scope, total),
            "pct_of_addressable": None,
            "by_reason":          out_of_scope_by_reason,
        },
        "bad_data": {  # PIPE-DQ-001: records with data quality flags
            "count":              bad_data_count,
            "pct_of_total":       pct(bad_data_count, total),
            "pct_of_addressable": None,
            "by_reason":          bad_data_by_reason,
        },
        "out_of_scope_flagged": {  # PIPE-DQ-002: records with out_of_scope flags
            "count":              out_of_scope_flagged_count,
            "pct_of_total":       pct(out_of_scope_flagged_count, total),
            "pct_of_addressable": None,
            "by_reason":          out_of_scope_flagged_by_reason,
        },
        "unenriched":       bucket([None]),
        "total_enriched": {
            "count":              _enriched_total,
            "pct_of_total":       pct(_enriched_total, total),
            "pct_of_addressable": pct(_enriched_total, addressable),
        },
        "total":       total,
        "addressable": addressable,
    }

    enrichment_sla = 98.0
    pct_enriched = summary["total_enriched"]["pct_of_addressable"] or 0.0
    succeeded = pct_enriched >= enrichment_sla
    job_status: dict = {"status": "succeed" if succeeded else "fail"}
    if not succeeded:
        job_status["fail_reason"] = (
            f"Enrichment SLA not met: {pct_enriched:.1f}% enriched "
            f"(required {enrichment_sla:.0f}% of addressable providers)"
        )

    report: dict = {"job_name": "County Enrichment", "job_status": job_status}
    report.update({
        "datetime": datetime.now(timezone.utc).isoformat(),
        "reconciliation": reconcile,
        "summary": summary,
        "pipeline_run": {"load_id": load_id},
    })

    client[db_name_r][coll_name_r].insert_one(report)
    report.pop("_id", None)
    logging.info(
        "Enrichment report — %d/%d addressable enriched (%.1f%%) | %d/%d total (%.1f%%)",
        summary["total_enriched"]["count"], addressable,
        summary["total_enriched"]["pct_of_addressable"],
        summary["total_enriched"]["count"], total,
        summary["total_enriched"]["pct_of_total"],
    )
    return report


