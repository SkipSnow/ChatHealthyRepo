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


SPLIT_THRESHOLD = 0.98
CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
CROSSWALK_COLLECTION = "PublicHealthData.ZipCountyCrosswalk"
PROVIDERS_COLLECTION = "PublicHealthData.providers_staging"


# ── Orchestrators ─────────────────────────────────────────────────────────────

def _build_enrichment_reconcile(pass1_result: dict, pass2_result: dict) -> dict:
    """Build combined reconcile dict from Pass 1 and Pass 2 orchestrator results.

    pass1_result may be empty ({}) when start_step > 3 skips Pass 1.
    In that case total_providers comes from pass2_result (set by get_unenriched_fn).
    """
    total_providers = (
        pass1_result.get("total_providers")
        or pass2_result.get("total_providers", 0)
    )
    pass1_modified         = pass1_result.get("pass1_modified", 0)
    pass2_modified         = pass2_result.get("pass2_modified", 0)
    pass2_billing_modified = pass2_result.get("pass2_billing_modified", 0)
    pass2_geocoder_failed  = pass2_result.get("pass2_geocoder_failed", pass2_result.get("pass2_failed", 0))
    pass2_no_address       = pass2_result.get("pass2_no_address", 0)
    pass2_failed           = pass2_geocoder_failed + pass2_no_address
    total_enriched = pass1_modified + pass2_modified + pass2_billing_modified
    still_unenriched = total_providers - total_enriched
    return {
        "total_providers": total_providers,
        "pass1_zip_enrichments": pass1_modified,
        "pass1_batch_results": pass1_result.get("pass1_batch_results", []),
        "pass2_address_lookups_attempted": pass2_modified + pass2_billing_modified + pass2_failed,
        "pass2_practice_enrichments": pass2_modified,
        "pass2_billing_enrichments": pass2_billing_modified,
        "pass2_geocoder_failed": pass2_geocoder_failed,
        "pass2_no_address": pass2_no_address,
        "pass2_address_lookups_failed": pass2_failed,
        "pass2_batch_results": pass2_result.get("pass2_batch_results", []),
        "total_enriched": total_enriched,
        "still_unenriched": still_unenriched,
        "match": still_unenriched == 0,
    }


def county_enrichment_pass1_orchestrator_fn(context):
    """Pass 1: ZIP-based bulk enrichment via ZipCountyCrosswalk.

    Ensures the county.fips index, computes confident ZIPs, and fans out
    one updateMany per ZIP. Returns results for the caller to combine with Pass 2.
    """
    config = context.get_input() or {}
    load_id = config.get("load_id", context.instance_id)
    config = {**config, "load_id": load_id}

    # Step 1: Ensure county.fips index. Idempotent — no-op if already exists.
    # Required when Pass 1 is run outside FullProviderPipeline.
    context.set_custom_status("Step 1/4: Ensuring county.fips index")
    yield context.call_activity("ensure_postload_indexes_activity", config)

    # Step 2: Get distinct ZIPs
    context.set_custom_status("Step 2/4: Getting distinct ZIPs from staging")
    zip_data = yield context.call_activity("get_distinct_zips_activity", config)
    total_providers = zip_data["total_providers"]
    distinct_zips = zip_data["distinct_zips"]

    # Step 3: Lookup crosswalk — split into confident vs ambiguous
    context.set_custom_status(f"Step 3/4: Looking up {len(distinct_zips):,} ZIPs in crosswalk")
    crosswalk_result = yield context.call_activity(
        "lookup_crosswalk_activity", {**config, "zips": distinct_zips}
    )
    confident_zips = crosswalk_result["confident"]

    # Step 4: Fan-out — one updateMany per ZIP
    num_workers = config.get("num_workers", 200)
    batch_size = max(1, len(confident_zips) // num_workers)
    zip_batches = [
        confident_zips[i:i + batch_size]
        for i in range(0, len(confident_zips), batch_size)
    ]
    context.set_custom_status(
        f"Step 4/4: {len(confident_zips):,} confident ZIPs across {len(zip_batches)} workers"
    )
    pass1_tasks = [
        context.call_activity("enrich_by_zip_batch_activity", {**config, "zip_batch": batch})
        for batch in zip_batches
    ]
    pass1_results = yield context.task_all(pass1_tasks)
    pass1_modified = sum(r.get("modified", 0) for r in pass1_results)

    context.set_custom_status(f"Done — {pass1_modified:,} enriched via ZIP crosswalk")
    return {
        "total_providers": total_providers,
        "confident_zips": len(confident_zips),
        "pass1_modified": pass1_modified,
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

    # Step 0a: Mark inactive and foreign providers as out_of_scope
    context.set_custom_status("Step 0/3: Marking inactive and foreign providers as out_of_scope")
    yield context.call_activity("mark_out_of_scope_activity", config)

    # Step 0b: Optional — reset geocoder_failed records so batch geocoder can retry them
    if config.get("reset_failed", False):
        context.set_custom_status("Step 0/3: Resetting geocoder_failed records for retry")
        yield context.call_activity("reset_geocoder_failed_activity", config)

    # Step 1: Get providers not yet enriched by Pass 1
    context.set_custom_status("Step 1/3: Getting unenriched providers")
    unenriched = yield context.call_activity("get_unenriched_activity", config)
    unenriched_count = unenriched["count"]
    unenriched_ids = unenriched["provider_ids"]

    # Step 2: Fan-out — one Census Geocoder call per provider
    addr_batch_size = config.get("addr_batch_size", 5_000)
    addr_batches = [
        unenriched_ids[i:i + addr_batch_size]
        for i in range(0, len(unenriched_ids), addr_batch_size)
    ]
    context.set_custom_status(
        f"Step 2/3: {unenriched_count:,} providers via Census Geocoder "
        f"across {len(addr_batches)} workers"
    )
    pass2_tasks = [
        context.call_activity("enrich_by_address_batch_activity", {**config, "id_batch": batch})
        for batch in addr_batches
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
    """Mark deactivated and foreign providers as out_of_scope before geocoding.

    These providers have county.fips = null but the Census geocoder cannot
    resolve them:
    - Deactivated: npi_deactivation_date set without a later reactivation date
    - Foreign: practice_address.country set (Census geocoder is US-only)

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
                # Deactivated with no reactivation
                {
                    "npi_deactivation_date": {"$exists": True},
                    "npi_reactivation_date": {"$exists": False},
                },
                # Foreign provider (country field absent = US in NPPES)
                {"practice_address.country": {"$exists": True}},
            ],
        },
        {"$set": {"county": {"fips": None, "source": "out_of_scope"}}},
    )
    logging.info(
        "Marked %d providers as out_of_scope (inactive or foreign)", result.modified_count
    )
    return {"marked_out_of_scope": result.modified_count}


def get_unenriched_fn(config: dict) -> dict:
    """Return _id list of providers without county.fips, plus total provider count.

    Excludes records already resolved or classified by a prior pass:
    geocoder_failed, geocoder_no_address, out_of_scope.
    mark_out_of_scope_fn must run before this to tag inactive and foreign providers.
    """
    collection = config.get("staging_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]
    total_providers = coll.count_documents({})
    ids = [
        str(doc["_id"])
        for doc in coll.find(
            {
                "county.fips": None,
                "county.source": {"$nin": [
                    "geocoder_failed", "geocoder_no_address", "out_of_scope"
                ]},
            },
            {"_id": 1}
        )
    ]
    logging.info("Unenriched providers for Pass 2: %d / %d total", len(ids), total_providers)
    return {"count": len(ids), "provider_ids": ids, "total_providers": total_providers}


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
    """Pass 2: Census Geocoder batch API — up to 5K providers per activity."""
    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()
    id_batch   = config["id_batch"]
    collection = config.get("staging_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    object_ids = [ObjectId(i) for i in id_batch]
    providers  = list(coll.find(
        {"_id": {"$in": object_ids}},
        {"_id": 1, "practice_address": 1, "mailing_address": 1},
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
    report.pop("_id", None)  # insert_one adds ObjectId in place; strip before returning
    logging.info(
        "Enrichment report — load_id: %s | "
        "Pass 1 ZIP: %d | "
        "Pass 2 practice: %d, billing fallback: %d | "
        "Pass 2 geocoder failed: %d, no address: %d | "
        "Total enriched: %d/%d | Match: %s",
        load_id,
        reconcile.get("pass1_zip_enrichments", 0),
        reconcile.get("pass2_practice_enrichments", 0),
        reconcile.get("pass2_billing_enrichments", 0),
        reconcile.get("pass2_geocoder_failed", 0),
        reconcile.get("pass2_no_address", 0),
        reconcile.get("total_enriched", 0),
        reconcile.get("total_providers", 0),
        reconcile.get("match"),
    )
    return report


