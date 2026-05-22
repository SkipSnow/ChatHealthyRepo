# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
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

    pass1_result may be empty ({}) when the parent orchestrator's start_step
    is later than Pass 1. still_unenriched and match are computed against
    addressable providers (total minus out_of_scope) so inactive/foreign
    providers do not prevent a complete match.
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

    # Idempotent — no-op if already exists. Required when Pass 1 runs outside FindCarePipeline.
    context.set_custom_status("Step 1: Ensuring county.fips index")
    yield context.call_activity("ensure_postload_indexes_activity", config)

    context.set_custom_status("Step 2: Marking inactive/foreign/deactivated providers as out_of_scope")
    out_of_scope_result = yield context.call_activity("mark_out_of_scope_activity", config)

    context.set_custom_status("Step 3: Marking ZIP/state mismatch providers as out_of_scope")
    zip_mismatch_result = yield context.call_activity("mark_zip_state_mismatch_activity", config)

    out_of_scope_count = (
        out_of_scope_result.get("marked_out_of_scope", 0)
        + zip_mismatch_result.get("marked_zip_state_mismatch", 0)
    )

    context.set_custom_status("Step 4: Getting distinct ZIPs from staging")
    zip_data = yield context.call_activity("get_distinct_zips_activity", config)
    total_providers = zip_data["total_providers"]
    distinct_zips = zip_data["distinct_zips"]

    context.set_custom_status("Step 5: Looking up ZIPs in crosswalk")
    crosswalk_result = yield context.call_activity(
        "lookup_crosswalk_activity", {**config, "zips": distinct_zips}
    )
    confident_zips = crosswalk_result["confident"]

    # Fan-out — one updateMany per ZIP (excludes out_of_scope providers)
    num_workers = config.get("num_workers", 200)
    batch_size = max(1, len(confident_zips) // num_workers)
    zip_batches = [
        confident_zips[i:i + batch_size]
        for i in range(0, len(confident_zips), batch_size)
    ]
    context.set_custom_status("Step 6: Bulk-enriching ZIPs across workers")
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
    """Reset practice_address elements flagged geocoder_failed so the batch
    geocoder can retry them. Operates per-element (multi-practice-address
    shape).

    Previous runs that used individual geocoder calls marked many elements as
    geocoder_failed due to rate limiting — not genuine address failures. This
    clears that flag so get_unenriched_fn picks them up again. Only call this
    when switching geocoder strategy; not on routine reruns.
    """
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    sf = _build_states_filter(config)
    result = _get_mongo_client()[db_name][coll_name].update_many(
        {
            "practice_address": {"$elemMatch": {"county.source": "geocoder_failed"}},
            "bad_data.flagged": {"$ne": True},
            "out_of_scope.flagged": {"$ne": True},
            **sf,
        },
        {"$set": {"practice_address.$[elem].county": {"fips": None}}},
        array_filters=[{"elem.county.source": "geocoder_failed"}],
    )
    logging.info("Reset geocoder_failed elements on %d providers for retry", result.modified_count)
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
        context.set_custom_status("Step 1: Resetting geocoder_failed records for retry")
        yield context.call_activity("reset_geocoder_failed_activity", config)

    context.set_custom_status("Step 2: Counting unenriched providers")
    unenriched = yield context.call_activity("get_unenriched_activity", config)
    unenriched_count = unenriched["count"]
    min_id = unenriched.get("min_id")
    max_id = unenriched.get("max_id")

    # Fan-out — partition by _id range, each activity queries its own slice.
    # _id range avoids skip() scans: each activity uses {_id: {$gte: start, $lt: end}}
    # directly on the _id index. Works at any scale without connection pressure.
    # 500 addresses per Census batch: responds in seconds, not minutes.
    addr_batch_size = config.get("addr_batch_size", 500)
    num_batches = math.ceil(unenriched_count / addr_batch_size) if unenriched_count else 0
    context.set_custom_status("Step 3: Census Geocoder enrichment fan-out")
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

    context.set_custom_status("Step 1: Finding geocoder_failed providers with billing addresses")
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
    context.set_custom_status("Step 2: Census Geocoder retry on billing address")
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

    context.set_custom_status("Step 1: Finding geocoder_failed providers for Maps retry")
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
    context.set_custom_status("Step 2: Google Maps Geocoding fan-out")
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
    FindCarePipeline calls Pass 1 and Pass 2 as separate visible steps instead.
    """
    config = context.get_input() or {}
    load_id = config.get("load_id", context.instance_id)
    config = {**config, "load_id": load_id}

    context.set_custom_status("Step 1: Pass 1 — ZIP bulk enrichment")
    pass1_result = yield context.call_sub_orchestrator(
        "county_enrichment_pass1_orchestrator", config
    )

    context.set_custom_status("Step 2: Pass 2 — Census Geocoder enrichment")
    pass2_result = yield context.call_sub_orchestrator(
        "county_enrichment_pass2_orchestrator", config
    )

    context.set_custom_status("Step 3: Writing enrichment report")
    reconcile = _build_enrichment_reconcile(pass1_result, pass2_result)
    yield context.call_activity("enrichment_report_activity", {**config, "reconcile": reconcile})

    status = "complete" if reconcile["match"] else "partial"
    context.set_custom_status(
        f"Done — {status}, {reconcile['total_enriched']:,}/{reconcile['total_providers']:,} enriched"
    )
    return reconcile


# ── Activity implementations ──────────────────────────────────────────────────

def get_distinct_zips_fn(config: dict) -> dict:
    """Return total provider count and list of distinct 5-digit ZIPs across
    every practice_address element of every in-scope provider.

    Handles both shapes (list of addresses post-multi-practice-address; legacy
    single-dict) by $unwind-ing practice_address — Mongo's $unwind treats a
    non-array field as a single-element list, so legacy records fall through
    the same path. preserveNullAndEmptyArrays=False drops records with no
    practice_address at all (those have no ZIP to enrich anyway)."""
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    state_filter = _build_states_filter(config)
    total = coll.count_documents(state_filter)
    pipeline = [
        {"$match": state_filter},
        {"$unwind": {"path": "$practice_address", "preserveNullAndEmptyArrays": False}},
        {"$project": {"zip5": {"$substr": [
            {"$ifNull": [{"$toString": "$practice_address.zip"}, ""]},
            0, 5
        ]}}},
        {"$group": {"_id": "$zip5"}},
    ]
    zips = [doc["_id"] for doc in coll.aggregate(pipeline, allowDiskUse=True) if doc["_id"]]
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
        self._state_filter = _build_states_filter(config)  # required
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
        county_subdoc = {
            "fips": entry["county_fips"],
            "name": entry["county_name"],
            "source": "crosswalk_pass1",
            "zip_ratio": entry["res_ratio"],
        }
        zip_pat = {"$regex": f"^{zip5}"}

        # Multi-practice-address support: enrich EVERY practice_address element
        # whose zip matches and which doesn't already have a county. Uses a
        # filtered positional update with arrayFilters so a provider with
        # several practice locations gets each one's county resolved
        # independently.
        #
        # The top-level `county` field is kept as a back-compat surface for
        # consumers that still read `doc.county.fips`. After every update we
        # reset top-level county to the PRIMARY's county (practice_address[0])
        # in a separate sync pass at the end of the worker (_pipeline_close
        # would be cleaner but the existing base class doesn't override-hook
        # there reliably).
        result = self._collection.update_many(
            {
                "bad_data.flagged": {"$ne": True},
                "out_of_scope.flagged": {"$ne": True},
                "practice_address": {"$elemMatch": {"zip": zip_pat, "county.fips": None}},
                **self._state_filter,
            },
            {"$set": {"practice_address.$[elem].county": county_subdoc}},
            array_filters=[{"elem.zip": zip_pat, "elem.county.fips": None}],
        )
        self._total_modified += result.modified_count

        # Legacy path: providers whose practice_address is still the old single-
        # dict shape (pre-multi-address commit) are matched by the original
        # query. Idempotent because the per-element pipeline above won't have
        # touched these (the $elemMatch requires an array). Both writes set
        # the same county; whichever runs first is fine.
        legacy = self._collection.update_many(
            {
                "county.fips": None,
                "county.source": {"$ne": "out_of_scope"},
                "bad_data.flagged": {"$ne": True},
                "out_of_scope.flagged": {"$ne": True},
                "practice_address": {"$type": "object", "$not": {"$type": "array"}},
                "practice_address.zip": zip_pat,
                **self._state_filter,
            },
            {"$set": {"county": county_subdoc}},
        )
        self._total_modified += legacy.modified_count

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

    sf = _build_states_filter(config)
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

    sf = _build_states_filter(config)

    # Normalize every dict-shape practice_address to a list-of-1 first.
    # After this, every record in scope has list-shape practice_address —
    # one canonical shape, one code path, no bifurcation downstream.
    norm = coll.update_many(
        {"practice_address": {"$type": "object", "$not": {"$type": "array"}}, **sf},
        [{"$set": {"practice_address": ["$practice_address"]}}],
    )
    if norm.modified_count:
        logging.info(
            "PIPE-DQ-003: normalized %d dict-shape practice_address records to list-of-1",
            norm.modified_count,
        )

    # Stream the cursor element-wise. Materializing the full result set with
    # list() blows the worker's heap on large multi-state runs (TX+CA+NY+VA+MS
    # ≈ 700K docs × ~10KB → ~7GB → OOM exit 137).
    cursor = coll.find(
        {
            "out_of_scope.flagged": {"$ne": True},
            "bad_data.flagged": {"$ne": True},
            "practice_address.state": {"$exists": True},
            "practice_address.zip":   {"$exists": True},
            **sf,
        },
        {"_id": 1, "practice_address": 1, "licenses": 1},
    )

    total_repaired = 0
    total_bad_data = 0
    ops: list = []

    for p in cursor:
        licenses = p.get("licenses") or []
        pa = p.get("practice_address") or []
        if not isinstance(pa, list):
            continue  # normalize() above ensures this never fires

        for idx, addr in enumerate(pa):
            if not isinstance(addr, dict):
                continue
            decision, reason, repaired_state = _evaluate_zip_state_mismatch(
                addr, licenses, zip_to_state,
            )
            if decision == "ok":
                continue
            if decision == "repair":
                ops.append(UpdateOne(
                    {"_id": p["_id"]},
                    {"$set": {f"practice_address.{idx}.state": repaired_state}},
                ))
                total_repaired += 1
                logging.info(
                    "PIPE-DQ-003: repaired provider %s element %d state→%s via license data",
                    p["_id"], idx, repaired_state,
                )
            else:  # "bad"
                ops.append(UpdateOne(
                    {"_id": p["_id"]},
                    {"$set": {f"practice_address.{idx}.county": {
                        "fips": None,
                        "source": "out_of_scope_zip_state_mismatch",
                        "reason": reason,
                    }}},
                ))
                total_bad_data += 1

        if len(ops) >= 1000:
            coll.bulk_write(ops, ordered=False)
            ops = []

    if ops:
        coll.bulk_write(ops, ordered=False)

    logging.info(
        "PIPE-DQ-003: %d addresses repaired via license, %d addresses flagged as bad_data (zip_state_mismatch)",
        total_repaired, total_bad_data,
    )
    return {
        "marked_zip_state_mismatch": total_bad_data,
        "repaired_via_license": total_repaired,
    }


def _evaluate_zip_state_mismatch(
    addr: dict,
    licenses: list,
    zip_to_state: dict,
) -> tuple:
    """Single-address evaluator. Called once per practice_address element.

    Returns (decision, reason, repaired_state):
      decision in {"ok", "repair", "bad"}
      reason is the bad_data reason string when decision == "bad", else None
      repaired_state is the new state value when decision == "repair", else None

    Logic (preserved from the pre-multi-address tested behavior):
      - No zip OR no state on this address → "ok" (nothing to check)
      - Zip not in crosswalk → "ok" (different concern; Pass 2-6 will try to geocode)
      - Zip's expected_state matches address.state → "ok"
      - Mismatch + no licenses → "bad" (zip_state_mismatch_no_license)
      - Mismatch + exactly one license state and it equals expected_state → "repair"
      - Mismatch + multiple license states with at least one matching → "bad" (zip_state_mismatch_multiple_licenses)
      - Mismatch + license data but none matches expected → "bad" (zip_state_mismatch)
    """
    zip5 = (addr.get("zip") or "")[:5]
    elem_state = (addr.get("state") or "").strip().upper()
    if not zip5 or not elem_state:
        return ("ok", None, None)
    expected = zip_to_state.get(zip5)
    if not expected:
        return ("ok", None, None)
    if expected == elem_state:
        return ("ok", None, None)

    license_states = {(lic.get("state") or "").strip().upper() for lic in licenses}
    license_states.discard("")
    if not license_states:
        return ("bad", "zip_state_mismatch_no_license", None)

    matching = {s for s in license_states if s == expected}
    if len(license_states) == 1 and len(matching) == 1:
        return ("repair", None, expected)
    if matching:
        return ("bad", "zip_state_mismatch_multiple_licenses", None)
    return ("bad", "zip_state_mismatch", None)


# ── Proprietary classification flags on providers ─────────────────────────
# can_prescribe and is_homeopathic are denormalized onto every provider
# record from the per-NUCC-code catalog stored in findcarestorage. Single
# source of truth = specialty_classification_catalog.load_catalog().


def apply_proprietary_flags_fn(config: dict) -> dict:
    """Stamp can_prescribe and is_homeopathic booleans on provider records.

    The provider's primary taxonomy code (or first, if no primary) is looked
    up in the proprietary catalog blob. Both flags are written as plain
    booleans. Organizations (entity_type_code "2") get can_prescribe=False
    and is_homeopathic=False regardless of taxonomy.

    Idempotent: providers whose `can_prescribe` already exists as a boolean
    are skipped via the {"$type": "bool"} filter. (Legacy nested-dict
    `can_prescribe.flagged` shape — written by an earlier dead code path —
    is intentionally re-stamped.)
    """
    from specialty_classification_catalog import load_catalog
    catalog = load_catalog()

    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    sf = _build_states_filter(config)
    base_filter = {
        "$or": [
            {"can_prescribe": {"$exists": False}},
            {"can_prescribe": {"$not": {"$type": "bool"}}},
            {"is_homeopathic": {"$exists": False}},
        ],
        **sf,
    }

    total = coll.count_documents(base_filter)
    logging.info("F5: %d providers need proprietary-flag stamping (catalog has %d entries)",
                 total, len(catalog))
    if total == 0:
        return {"classified": 0, "prescribers": 0, "homeopathic": 0,
                "unknown_taxonomy": 0}

    ops = []
    prescribers = 0
    homeopathic = 0
    unknown_taxonomy = 0

    cursor = coll.find(
        base_filter,
        {"npi": 1, "entity_type_code": 1, "taxonomies": 1, "_id": 1},
    )

    for doc in cursor:
        entity_type = doc.get("entity_type_code", "")
        if entity_type == "2":
            can_prescribe = False
            is_homeopathic = False
        else:
            taxonomies = doc.get("taxonomies", []) or []
            primary = next((t for t in taxonomies if t.get("primary")), None)
            tax = primary or (taxonomies[0] if taxonomies else {})
            tax_code = tax.get("code", "") if isinstance(tax, dict) else ""
            entry = catalog.get(tax_code)
            if entry is None:
                # Taxonomy code not in catalog → cannot classify.
                # Default both flags to False and count separately.
                can_prescribe = False
                is_homeopathic = False
                unknown_taxonomy += 1
            else:
                can_prescribe = entry["can_prescribe"]
                is_homeopathic = entry["is_homeopathic"]

        if can_prescribe:
            prescribers += 1
        if is_homeopathic:
            homeopathic += 1

        ops.append(UpdateOne(
            {"_id": doc["_id"]},
            {"$set": {
                "can_prescribe": can_prescribe,
                "is_homeopathic": is_homeopathic,
            }},
        ))

        if len(ops) >= 1000:
            coll.bulk_write(ops, ordered=False)
            ops = []

    if ops:
        coll.bulk_write(ops, ordered=False)

    logging.info("F5: classified %d providers — prescribers: %d, homeopathic: %d, "
                 "unknown_taxonomy: %d", total, prescribers, homeopathic, unknown_taxonomy)
    return {
        "classified": total,
        "prescribers": prescribers,
        "homeopathic": homeopathic,
        "unknown_taxonomy": unknown_taxonomy,
    }


# Per-element county.source values that mean "do not touch this element" —
# either it's been geocoded already, classified as out-of-scope, or has bad
# source data. Pass 1's crosswalk and Passes 2-6 all skip elements with any
# of these source values via array_filters / $elemMatch.
_CLASSIFIED_ELEMENT_SOURCES = [
    "geocoder_failed",
    "geocoder_no_address",
    "out_of_scope",
    "out_of_scope_zip_state_mismatch",
]


_UNENRICHED_FILTER = {
    # At least one practice_address element still needs county and hasn't been
    # classified by a prior pass. Multi-practice-address shape (list of dicts).
    # Legacy single-dict-shape records are skipped here — Pass 1 has its own
    # legacy path that handles them; passes 2-6 only target list-shape records.
    "practice_address": {"$elemMatch": {
        "county.fips": None,
        "county.source": {"$nin": _CLASSIFIED_ELEMENT_SOURCES},
    }},
    "bad_data.flagged": {"$ne": True},
    "out_of_scope.flagged": {"$ne": True},
}


def _unenriched_elements(provider: dict):
    """Yield (idx, addr_dict) for each practice_address element of `provider`
    that still needs county enrichment (county.fips null and not classified
    as geocoder_failed / geocoder_no_address / out_of_scope).

    Yields nothing for legacy single-dict shape — those are handled by Pass 1's
    legacy path. Passes 2-6 operate on list-shape records exclusively.
    """
    pa = provider.get("practice_address")
    if not isinstance(pa, list):
        return
    for idx, addr in enumerate(pa):
        if not isinstance(addr, dict):
            continue
        c = addr.get("county") or {}
        if c.get("fips"):
            continue
        if c.get("source") in ("geocoder_failed", "geocoder_no_address", "out_of_scope"):
            continue
        yield idx, addr


def _failed_elements(provider: dict):
    """Yield (idx, addr_dict) for each practice_address element flagged
    geocoder_failed by an earlier pass. Used by Pass 3/4 retry logic."""
    pa = provider.get("practice_address")
    if not isinstance(pa, list):
        return
    for idx, addr in enumerate(pa):
        if not isinstance(addr, dict):
            continue
        if (addr.get("county") or {}).get("source") == "geocoder_failed":
            yield idx, addr


def _census_batch_geocode(rows: list) -> dict:
    """rows: list of (composite_id_str, line1, city, state, zip5). Returns
    dict[composite_id_str -> "5-digit-FIPS"] for matches only. Raises on
    HTTP/network errors so the caller can leave elements unenriched for retry.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
    for cid, line1, city, state, zip5 in rows:
        writer.writerow([cid, line1, city, state, zip5])
    resp = requests.post(
        CENSUS_BATCH_URL,
        files={"addressFile": ("addresses.csv", buf.getvalue().encode("utf-8"), "text/csv")},
        data={"benchmark": "Public_AR_Current", "vintage": "Current_Current"},
        timeout=300,
    )
    resp.raise_for_status()
    matched: dict = {}
    for row in csv.reader(io.StringIO(resp.text)):
        if len(row) < 10:
            continue
        cid, match, state_fp, county_fp = row[0].strip(), row[2].strip(), row[8].strip(), row[9].strip()
        if match not in ("Match", "Tie") or not state_fp or not county_fp:
            continue
        matched.setdefault(cid, state_fp + county_fp)
    logging.info("Census batch geocoder: %d/%d matched", len(matched), len(rows))
    return matched


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
    sf = _build_states_filter(config)
    enrichment_filter = {**_UNENRICHED_FILTER, **sf}
    total_providers = coll.count_documents(sf)
    unenriched = coll.count_documents(enrichment_filter)
    first = coll.find_one(enrichment_filter, {"_id": 1}, sort=[("_id", 1)])
    last  = coll.find_one(enrichment_filter, {"_id": 1}, sort=[("_id", -1)])
    min_id = str(first["_id"]) if first else None
    max_id = str(last["_id"])  if last  else None
    logging.info("Unenriched providers for Pass 2: %d / %d total (states: %s)", unenriched, total_providers, sf)
    return {"count": unenriched, "total_providers": total_providers, "min_id": min_id, "max_id": max_id}


def enrich_by_address_batch_fn(config: dict) -> dict:
    """Pass 2: Census Geocoder batch API — per-practice-address-element.

    For each provider in this _id range, iterates every practice_address element
    that still needs a county. Each element gets one CSV row in the Census batch
    keyed by composite ID `<provider_oid>_<elem_idx>`. Results are written back
    via per-element `practice_address.<idx>.county` updates.

    Elements with no usable address (no line1 and no city) are flagged
    `geocoder_no_address`. Elements the geocoder couldn't match are flagged
    `geocoder_failed` (Pass 3 will retry these via the doc's mailing address).

    Billing-fallback that the old Pass 2 did per-provider has moved entirely to
    Pass 3 — Pass 2 now ONLY tries the practice address.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()
    start_id_hex = config["start_id"]
    end_id_hex   = config.get("end_id")
    collection   = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    sf = _build_states_filter(config)
    id_filter: dict = {"_id": {"$gte": ObjectId(start_id_hex)}}
    if end_id_hex:
        id_filter["_id"]["$lt"] = ObjectId(end_id_hex)

    providers = list(coll.find(
        {**_UNENRICHED_FILTER, **id_filter, **sf},
        {"_id": 1, "practice_address": 1},
    ))

    # Per-element fan-out
    geocode_rows: list = []                           # (cid, line1, city, state, zip5)
    composite_to_pidx: dict[str, tuple] = {}          # cid -> (provider_oid, elem_idx)
    ops: list = []
    no_address = 0

    for p in providers:
        pid_obj = p["_id"]
        pid_str = str(pid_obj)
        for idx, addr in _unenriched_elements(p):
            line1 = (addr.get("line1") or "").strip()
            city  = (addr.get("city")  or "").strip()
            state = (addr.get("state") or "").strip()
            zip5  = (addr.get("zip")   or "").strip()[:5]
            if line1 or city:
                cid = f"{pid_str}_{idx}"
                geocode_rows.append((cid, line1, city, state, zip5))
                composite_to_pidx[cid] = (pid_obj, idx)
            else:
                ops.append(UpdateOne(
                    {"_id": pid_obj},
                    {"$set": {f"practice_address.{idx}.county": {
                        "fips": None, "source": "geocoder_no_address",
                    }}},
                ))
                no_address += 1

    modified = geocoder_failed = 0
    if geocode_rows:
        batch_ok = False
        matched: dict = {}
        try:
            matched = _census_batch_geocode(geocode_rows)
            batch_ok = True
        except Exception as exc:
            logging.error(
                "Pass 2 Census batch geocoder failed (%d elements left for retry): %s",
                len(geocode_rows), exc,
            )

        if batch_ok:
            for cid, *_ in geocode_rows:
                pid_obj, idx = composite_to_pidx[cid]
                if cid in matched:
                    fips = matched[cid]
                    ops.append(UpdateOne(
                        {"_id": pid_obj},
                        {"$set": {f"practice_address.{idx}.county": {
                            "fips": fips,
                            "name": _get_fips_to_name().get(fips, ""),
                            "source": "geocoder_pass2_batch",
                        }}},
                    ))
                    modified += 1
                else:
                    ops.append(UpdateOne(
                        {"_id": pid_obj},
                        {"$set": {f"practice_address.{idx}.county": {
                            "fips": None, "source": "geocoder_failed",
                        }}},
                    ))
                    geocoder_failed += 1

    if ops:
        coll.bulk_write(ops, ordered=False)

    return {
        "assigned":         len(providers),
        "succeeded":        modified,
        "modified":         modified,
        "billing_modified": 0,            # back-compat key — billing moved to Pass 3
        "geocoder_failed":  geocoder_failed,
        "no_address":       no_address,
        "started_at":       started_at,
        "finished_at":      datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - start_time, 2),
    }


def get_billing_retryable_fn(config: dict) -> dict:
    """Return _id list of providers that have at least one practice_address
    element flagged geocoder_failed AND a usable mailing/billing address.

    Excludes deactivated and foreign providers (same rules as Pass 2).
    """
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    sf = _build_states_filter(config)
    ids = [
        str(doc["_id"])
        for doc in coll.find(
            {
                "practice_address": {"$elemMatch": {"county.source": "geocoder_failed"}},
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
    """Pass 3: For each provider with one or more geocoder_failed practice_address
    elements, geocode the doc's mailing/billing address ONCE. On match, apply
    that billing-derived county to every element flagged geocoder_failed.

    Rationale: the doc has a single mailing address; a successful billing
    geocode gives one fallback county we can apply to every still-unresolved
    practice location. Element-specific geocoding (per-element address) is
    Pass 4's job (Maps).
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
        {"_id": 1, "mailing_address": 1, "practice_address": 1},
    ))

    geocodable = []  # list of (pid_str, mailing_dict, provider_doc)
    for p in providers:
        m = p.get("mailing_address") or {}
        if m.get("line1") or m.get("city"):
            geocodable.append((str(p["_id"]), m, p))

    modified_elements = providers_failed = 0
    ops: list = []

    if geocodable:
        rows = []
        for pid, m, _p in geocodable:
            line1 = (m.get("line1") or "").strip()
            city  = (m.get("city")  or "").strip()
            state = (m.get("state") or "").strip()
            zip5  = (m.get("zip")   or "").strip()[:5]
            rows.append((pid, line1, city, state, zip5))

        batch_ok = False
        matched: dict = {}
        try:
            matched = _census_batch_geocode(rows)
            batch_ok = True
        except Exception as exc:
            logging.error(
                "Pass 3 billing batch geocoder failed (%d providers left for retry): %s",
                len(rows), exc,
            )

        if batch_ok:
            for pid_str, _m, p in geocodable:
                if pid_str not in matched:
                    providers_failed += 1
                    continue
                fips = matched[pid_str]
                county_subdoc = {
                    "fips": fips,
                    "name": _get_fips_to_name().get(fips, ""),
                    "source": "geocoder_pass3_billing",
                }
                # Apply to every still-failed element on this provider.
                for idx, _addr in _failed_elements(p):
                    ops.append(UpdateOne(
                        {"_id": p["_id"]},
                        {"$set": {f"practice_address.{idx}.county": county_subdoc}},
                    ))
                    modified_elements += 1

    if ops:
        coll.bulk_write(ops, ordered=False)

    logging.info(
        "Pass 3 billing batch: %d elements matched across providers, %d providers still failed",
        modified_elements, providers_failed,
    )
    return {
        "assigned":         len(providers),
        "succeeded":        modified_elements,
        "modified":         modified_elements,
        "geocoder_failed":  providers_failed,
        "started_at":       started_at,
        "finished_at":      datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - start_time, 2),
    }


def get_maps_retryable_fn(config: dict) -> dict:
    """Return _id list of providers with at least one practice_address element
    flagged geocoder_failed (eligible for per-element Google Maps retry)."""
    collection = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    sf = _build_states_filter(config)
    ids = [
        str(doc["_id"])
        for doc in coll.find(
            {
                "practice_address": {"$elemMatch": {"county.source": "geocoder_failed"}},
                "bad_data.flagged": {"$ne": True},
                "out_of_scope.flagged": {"$ne": True},
                **sf,
            },
            {"_id": 1},
        )
    ]
    logging.info("Pass 4 Maps-retryable providers: %d", len(ids))
    return {"count": len(ids), "provider_ids": ids}


def enrich_by_maps_batch_fn(config: dict) -> dict:
    """Pass 4: Google Maps Geocoding API per failed practice_address element.

    For each provider in id_batch, iterates every practice_address element
    flagged geocoder_failed and calls Maps for that element's address. Resolves
    Maps (county_name, state_abbr) -> FIPS via ZipCountyCrosswalk and writes
    per-element `practice_address.<idx>.county`. Elements that Maps can't
    resolve stay flagged geocoder_failed (Pass 6 / NPPES is the next try).

    Rate-limited by maps_call_delay_seconds (default 0.1 -> ~10 calls/s/worker).
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
        {"_id": 1, "practice_address": 1},
    ))

    lookup = _get_maps_county_lookup()
    ops: list = []
    modified = maps_failed = 0

    for p in providers:
        for idx, addr in _failed_elements(p):
            line1 = (addr.get("line1") or "").strip()
            city  = (addr.get("city")  or "").strip()
            state = (addr.get("state") or "").strip()
            zip5  = (addr.get("zip")   or "").strip()[:5]
            address = ", ".join(part for part in (line1, city, state, zip5) if part)

            if not address:
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
                    {"$set": {f"practice_address.{idx}.county": {
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

    logging.info("Pass 4 Maps batch: %d elements matched, %d still failed", modified, maps_failed)
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

    Delegates to the shared state_filter helper so the predicate is identical to
    the one drain and load use (EPIC-010-F-006-S-001 REQ-T-001 uniform predicate,
    REQ-T-002 raise on missing, REQ-T-004 ALL sentinel returns {} = match all).
    """
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(__file__))
    from state_filter import normalize_states, mongo_state_filter
    return mongo_state_filter(normalize_states(config))




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
        "practice_address": {"$elemMatch": {"county.fips": None}},
        "bad_data.flagged": {"$ne": True},
        "out_of_scope.flagged": {"$ne": True},
        "npi": {"$nin": [None, ""]},
        **_build_states_filter(config),  # mandatory
    }

    providers = [
        {"id": str(doc["_id"]), "npi": doc["npi"]}
        for doc in coll.find(query, {"_id": 1, "npi": 1})
    ]
    logging.info("Pass 6 NPPES-retryable providers: %d", len(providers))
    return {"count": len(providers), "providers": providers}


def enrich_by_nppes_batch_fn(config: dict) -> dict:
    """Pass 6: NPPES public registry lookup, per-element.

    For each provider in provider_batch with at least one practice_address
    element still missing county, calls the NPPES API ONCE per NPI to fetch
    the canonical CMS-registered addresses. Then for each unenriched element
    (county.fips null), looks for an NPPES address with a different ZIP from
    the element's ZIP and the same state — applies the ZIP crosswalk to that
    NPPES ZIP and writes the per-element county.

    Skips elements where the only matching NPPES address has the same ZIP we
    already failed to resolve (no benefit to retry).

    Rate-limited by nppes_call_delay_seconds (default 0.2 s = 5 calls/s).
    Keep nppes_batch_size large so few activities fan out — workers share the
    same outbound IP and hit the same NPPES rate limit.

    county.source values written:
      geocoder_pass6_nppes  - resolved via NPPES canonical address + crosswalk
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

        nppes_locations = [
            a for a in results[0].get("addresses", [])
            if a.get("address_purpose") == "LOCATION"
        ]
        if not nppes_locations:
            # Fall back to whatever NPPES has
            nppes_locations = results[0].get("addresses", [])

        if not nppes_locations:
            nppes_failed += 1
            if delay:
                time.sleep(delay)
            continue

        # Per-element retry: for each unenriched element, try to find a
        # different ZIP in NPPES (same state) and apply the crosswalk.
        any_modified = False
        for idx, addr in _unenriched_elements(doc):
            our_zip   = (addr.get("zip") or "")[:5].strip()
            our_state = (addr.get("state") or "").upper().strip()

            candidate = None
            for n in nppes_locations:
                n_zip   = (n.get("postal_code") or "")[:5].strip()
                n_state = (n.get("state") or "").upper().strip()
                if not n_zip:
                    continue
                if our_state and n_state and n_state != our_state:
                    continue
                if n_zip == our_zip:
                    continue  # same ZIP — won't help
                candidate = n
                break

            if candidate is None:
                nppes_same_address += 1
                continue

            n_zip   = (candidate.get("postal_code") or "")[:5].strip()
            n_state = (candidate.get("state") or "").upper().strip()
            cw = crosswalk.get(n_zip)
            if not cw or cw.get("is_split"):
                nppes_failed += 1
                continue
            state_fips = _STATE_ABBR_TO_FIPS.get(n_state)
            if not state_fips or cw["fips"][:2] != state_fips:
                nppes_failed += 1
                continue

            ops.append(UpdateOne(
                {"_id": ObjectId(pid)},
                {"$set": {f"practice_address.{idx}.county": {
                    "fips": cw["fips"],
                    "name": cw.get("name", ""),
                    "source": "geocoder_pass6_nppes",
                }}},
            ))
            modified += 1
            any_modified = True

        if delay:
            time.sleep(delay)

    if ops:
        coll.bulk_write(ops, ordered=False)

    logging.info(
        "Pass 6 NPPES batch: %d elements matched, %d same address skipped, "
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

    context.set_custom_status(f"Step 1: Finding NPPES-retryable providers{states_label}")
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
    context.set_custom_status(f"Step 2: NPPES registry lookup fan-out{states_label}")
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
    sf = _build_states_filter(config)  # mandatory state filter

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


