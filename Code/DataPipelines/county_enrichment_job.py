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

  Pass 2: Census Geocoder batch API for providers whose ZIP is split
          (res_ratio < 0.98) or not found in the crosswalk.
          Sends up to 5,000 addresses per batch POST — ~100 activities total
          instead of ~10K individual calls. On error, providers are left
          unenriched and retried on the next run.
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
        coll = _get_mongo_client()["PublicHealthData"]["ZipCountyCrosswalk"]
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


def _get_maps_county_lookup() -> dict:
    """Build (state_fips_2d, county_name_lower) → 5-digit county_fips from the crosswalk.

    Used by Pass 4 to resolve county names returned by Google Maps into FIPS codes.
    Built lazily and cached per process lifetime.
    """
    global _maps_county_lookup
    if _maps_county_lookup is None:
        coll = _get_mongo_client()["PublicHealthData"]["ZipCountyCrosswalk"]
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
CROSSWALK_COLLECTION = "PublicHealthData.ZipCountyCrosswalk"
PROVIDERS_COLLECTION = "PublicHealthData.providers_staging"

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
) -> dict:
    """Build combined reconcile dict from Pass 1–4 results.

    pass1_result may be empty ({}) when start_step > 3 skips Pass 1.
    still_unenriched and match are computed against addressable providers
    (total minus out_of_scope) so inactive/foreign providers do not prevent
    a complete match.
    """
    pass3_result = pass3_result or {}
    pass4_result = pass4_result or {}
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
    total_enriched         = pass1_modified + pass2_modified + pass2_billing_modified + pass3_modified + pass4_modified
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
    context.set_custom_status("Step 1/5: Ensuring county.fips index")
    yield context.call_activity("ensure_postload_indexes_activity", config)

    # Step 2: Mark inactive and foreign providers as out_of_scope before any enrichment.
    # Pass 1's updateMany excludes out_of_scope so these are never enriched.
    context.set_custom_status("Step 2/5: Marking inactive and foreign providers as out_of_scope")
    out_of_scope_result = yield context.call_activity("mark_out_of_scope_activity", config)
    out_of_scope_count = out_of_scope_result.get("marked_out_of_scope", 0)

    # Step 3: Get distinct ZIPs
    context.set_custom_status("Step 3/5: Getting distinct ZIPs from staging")
    zip_data = yield context.call_activity("get_distinct_zips_activity", config)
    total_providers = zip_data["total_providers"]
    distinct_zips = zip_data["distinct_zips"]

    # Step 4: Lookup crosswalk — split into confident vs ambiguous
    context.set_custom_status(f"Step 4/5: Looking up {len(distinct_zips):,} ZIPs in crosswalk")
    crosswalk_result = yield context.call_activity(
        "lookup_crosswalk_activity", {**config, "zips": distinct_zips}
    )
    confident_zips = crosswalk_result["confident"]

    # Step 5: Fan-out — one updateMany per ZIP (excludes out_of_scope providers)
    num_workers = config.get("num_workers", 200)
    batch_size = max(1, len(confident_zips) // num_workers)
    zip_batches = [
        confident_zips[i:i + batch_size]
        for i in range(0, len(confident_zips), batch_size)
    ]
    context.set_custom_status(
        f"Step 5/5: {len(confident_zips):,} confident ZIPs across {len(zip_batches)} workers"
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
    collection = config.get("staging_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    result = _get_mongo_client()[db_name][coll_name].update_many(
        {"county.source": "geocoder_failed"},
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

    # Step 1: Count unenriched providers (count only — no ID list to avoid size limits)
    context.set_custom_status("Step 1/2: Counting unenriched providers")
    unenriched = yield context.call_activity("get_unenriched_activity", config)
    unenriched_count = unenriched["count"]

    # Step 2: Fan-out — each activity fetches its own slice via skip/limit
    addr_batch_size = config.get("addr_batch_size", 5_000)
    num_batches = math.ceil(unenriched_count / addr_batch_size) if unenriched_count else 0
    context.set_custom_status(
        f"Step 2/2: {unenriched_count:,} providers via Census Geocoder "
        f"across {num_batches} workers"
    )
    pass2_tasks = [
        context.call_activity("enrich_by_address_batch_activity", {**config, "batch_index": i, "addr_batch_size": addr_batch_size})
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
    collection = config.get("staging_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    total = coll.count_documents({})
    pipeline = [
        {"$project": {"zip5": {"$substr": [
            {"$ifNull": [{"$toString": "$practice_address.zip"}, ""]},
            0, 5
        ]}}},
        {"$group": {"_id": "$zip5"}},
    ]
    zips = [doc["_id"] for doc in coll.aggregate(pipeline) if doc["_id"]]
    logging.info("Found %d providers, %d distinct ZIPs", total, len(zips))
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
        self.staging_collection = config.get("staging_collection", PROVIDERS_COLLECTION)
        self._idx: int = -1
        self._collection = None
        self._total_modified: int = 0
        self._started_at: str = ""
        self._start_time: float = 0.0

    def _pipeline_open(self) -> None:
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._start_time = time.monotonic()
        db_name, coll_name = self.staging_collection.split(".", 1)
        self._collection = _get_mongo_client()[db_name][coll_name]

    def _pipeline_has_next(self) -> bool:
        self._idx += 1
        return self._idx < len(self.zip_batch)

    def _pipeline_process(self) -> None:
        entry = self.zip_batch[self._idx]
        zip5 = entry["zip"]
        result = self._collection.update_many(
            {
                "county.fips": None,
                "county.source": {"$ne": "out_of_scope"},
                "practice_address.zip": {"$regex": f"^{zip5}"},
            },
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
    """Mark providers the Census geocoder cannot resolve as out_of_scope.

    Three conditions:
    - No address: both practice_address and mailing_address are absent —
      no location to geocode.
    - Foreign: practice_address.country is set and is not "US" (NPPES sets
      "US" for all domestic providers; non-US values indicate foreign providers).
    - Deactivated: npi_deactivation_date set without a later reactivation date.

    Sets county = {fips: null, source: "out_of_scope"} so they are excluded
    from all subsequent geocoder passes and clearly visible in reporting.
    """
    collection = config.get("staging_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    result = coll.update_many(
        {
            "county.fips": None,
            "county.source": {"$nin": ["geocoder_failed", "geocoder_no_address", "out_of_scope"]},
            "$or": [
                # No address in either container — Census geocoder has nothing to work with
                {
                    "practice_address": {"$exists": False},
                    "mailing_address": {"$exists": False},
                },
                # Foreign provider (NPPES sets "US" for domestic; flag only non-US)
                {"practice_address.country": {"$exists": True, "$ne": "US"}},
                # Deactivated with no reactivation
                {
                    "npi_deactivation_date": {"$exists": True},
                    "npi_reactivation_date": {"$exists": False},
                },
            ],
        },
        {"$set": {"county": {"fips": None, "source": "out_of_scope"}}},
    )
    logging.info(
        "Marked %d providers as out_of_scope (no address, foreign, or deactivated)", result.modified_count
    )
    return {"marked_out_of_scope": result.modified_count}


_UNENRICHED_FILTER = {
    "county.fips": None,
    "county.source": {"$nin": ["geocoder_failed", "geocoder_no_address", "out_of_scope"]},
}


def get_unenriched_fn(config: dict) -> dict:
    """Return count of providers without county.fips, plus total provider count.

    Returns count only — no ID list — so the result stays small regardless of scale.
    Pass 2 fan-out uses skip/limit to let each activity fetch its own slice directly.

    Excludes records already resolved or classified by a prior pass:
    geocoder_failed, geocoder_no_address, out_of_scope.
    mark_out_of_scope_fn must run before this.
    """
    collection = config.get("staging_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    total_providers = coll.count_documents({})
    unenriched = coll.count_documents(_UNENRICHED_FILTER)
    logging.info("Unenriched providers for Pass 2: %d / %d total", unenriched, total_providers)
    return {"count": unenriched, "total_providers": total_providers}


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
    batch_index      = config["batch_index"]
    addr_batch_size  = config.get("addr_batch_size", 5_000)
    skip             = batch_index * addr_batch_size
    collection       = config.get("staging_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    providers = list(coll.find(
        _UNENRICHED_FILTER,
        {"_id": 1, "practice_address": 1, "mailing_address": 1},
        sort=[("_id", 1)],
    ).skip(skip).limit(addr_batch_size))

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
                        {"$set": {"county": {"fips": r["fips"], "source": r["source"]}}},
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

    return {
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
    collection = config.get("staging_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    ids = [
        str(doc["_id"])
        for doc in coll.find(
            {
                "county.source": "geocoder_failed",
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
                "mailing_address.country": {"$exists": False},
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
    collection = config.get("staging_collection", PROVIDERS_COLLECTION)
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
                        {"$set": {"county": {"fips": matched[pid], "source": "geocoder_pass3_billing"}}},
                    ))
                    modified += 1
                else:
                    geocoder_failed += 1  # leave county.source = geocoder_failed unchanged

    if ops:
        coll.bulk_write(ops, ordered=False)

    logging.info("Pass 3 billing batch: %d matched, %d still failed", modified, geocoder_failed)
    return {
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
    collection = config.get("staging_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    ids = [
        str(doc["_id"])
        for doc in coll.find(
            {
                "county.source": "geocoder_failed",
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
    collection = config.get("staging_collection", PROVIDERS_COLLECTION)
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
                {"$set": {"county": {"fips": fips, "source": "geocoder_pass4_maps"}}},
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
        "modified":         modified,
        "maps_failed":      maps_failed,
        "started_at":       started_at,
        "finished_at":      datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - start_time, 2),
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
    staging_collection = config.get("staging_collection", PROVIDERS_COLLECTION)
    db_name_r, coll_name_r = report_collection.split(".", 1)
    db_name_s, coll_name_s = staging_collection.split(".", 1)

    client = _get_mongo_client()

    # Live count by county.source (null source = never touched)
    source_counts: dict = {
        doc["_id"]: doc["count"]
        for doc in client[db_name_s][coll_name_s].aggregate([
            {"$group": {"_id": "$county.source", "count": {"$sum": 1}}}
        ])
    }

    total      = sum(source_counts.values())
    out_of_scope = source_counts.get("out_of_scope", 0)
    addressable  = total - out_of_scope  # providers the geocoder can reach

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
    ]
    _enriched_total = sum(source_counts.get(s, 0) for s in _enriched_sources)

    summary = {
        "pass1_zip":        bucket(["crosswalk_pass1"]),
        "pass2_individual": bucket(["geocoder_pass2", "geocoder_pass2_billing"]),
        "pass2_batch":      bucket(["geocoder_pass2_batch", "geocoder_pass2_batch_billing"]),
        "pass3_billing":    bucket(["geocoder_pass3_billing"]),
        "pass4_maps":       bucket(["geocoder_pass4_maps"]),
        "geocoder_failed":  bucket(["geocoder_failed"]),
        "no_address":       bucket(["geocoder_no_address"]),
        "out_of_scope":     {   # pct_of_addressable is N/A — these ARE the excluded set
            "count":              out_of_scope,
            "pct_of_total":       pct(out_of_scope, total),
            "pct_of_addressable": None,
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


