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
CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/geographies/address"
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


def county_enrichment_pass2_orchestrator_fn(context):
    """Pass 2: Census Geocoder enrichment for providers with split or unknown ZIPs.

    Queries providers still missing county.fips, fans out Census Geocoder
    lookups, and returns results for the caller to combine with Pass 1.
    """
    config = context.get_input() or {}
    load_id = config.get("load_id", context.instance_id)
    config = {**config, "load_id": load_id}

    # Step 1: Get providers not yet enriched by Pass 1
    context.set_custom_status("Step 1/2: Getting unenriched providers")
    unenriched = yield context.call_activity("get_unenriched_activity", config)
    unenriched_count = unenriched["count"]
    unenriched_ids = unenriched["provider_ids"]

    # Step 2: Fan-out — one Census Geocoder call per provider
    addr_batch_size = config.get("addr_batch_size", 50)
    addr_batches = [
        unenriched_ids[i:i + addr_batch_size]
        for i in range(0, len(unenriched_ids), addr_batch_size)
    ]
    context.set_custom_status(
        f"Step 2/2: {unenriched_count:,} providers via Census Geocoder "
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


def get_unenriched_fn(config: dict) -> dict:
    """Return _id list of providers without county.fips, plus total provider count.

    Excludes records already attempted by Pass 2 (county.source = geocoder_failed
    or geocoder_no_address) — these are permanently unresolvable and must not be
    re-attempted on subsequent runs.
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
                "county.source": {"$nin": ["geocoder_failed", "geocoder_no_address"]},
            },
            {"_id": 1}
        )
    ]
    logging.info("Unenriched providers for Pass 2: %d / %d total", len(ids), total_providers)
    return {"count": len(ids), "provider_ids": ids, "total_providers": total_providers}


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
            "fips": state_fips + county_fips_suffix,
            "name": county.get("NAME", ""),
        }
    except Exception as exc:
        logging.warning("Census Geocoder failed for %s %s: %s", street, zip_code, exc)
        return None


class AddressEnrichmentWorker(PipelineWorkerBase):
    """Pass 2: Census Geocoder enrichment per provider.

    Pass 2b: when practice_address is blank, falls back to mailing_address.
    Source provenance:
      geocoder_pass2         — practice address geocoded successfully
      geocoder_pass2_billing — mailing address geocoded successfully (Pass 2b fallback)
      geocoder_failed        — geocoder returned no match on best available address
      geocoder_no_address    — both practice and mailing addresses are blank
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.id_batch = config["id_batch"]
        self.staging_collection = config.get("staging_collection", PROVIDERS_COLLECTION)
        self._providers: list = []
        self._idx: int = -1
        self._collection = None
        self._ops: list = []
        self._modified: int = 0
        self._billing_modified: int = 0
        self._geocoder_failed: int = 0
        self._no_address: int = 0
        self._started_at: str = ""
        self._start_time: float = 0.0

    def _pipeline_open(self) -> None:
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._start_time = time.monotonic()
        db_name, coll_name = self.staging_collection.split(".", 1)
        self._collection = _get_mongo_client()[db_name][coll_name]
        object_ids = [ObjectId(i) for i in self.id_batch]
        self._providers = list(self._collection.find(
            {"_id": {"$in": object_ids}},
            {"_id": 1, "practice_address": 1, "mailing_address": 1}
        ))

    def _pipeline_has_next(self) -> bool:
        self._idx += 1
        return self._idx < len(self._providers)

    def _pipeline_process(self) -> None:
        provider = self._providers[self._idx]

        # ── Resolve best available address ────────────────────────────────────
        practice = provider.get("practice_address") or {}
        street   = practice.get("line1", "").strip()
        city     = practice.get("city",  "").strip()
        state    = practice.get("state", "").strip()
        zip_code = practice.get("zip",  "").strip()
        using_billing = False

        if not street and not city:
            # Pass 2b: fall back to mailing address
            mailing  = provider.get("mailing_address") or {}
            street   = mailing.get("line1", "").strip()
            city     = mailing.get("city",  "").strip()
            state    = mailing.get("state", "").strip()
            zip_code = mailing.get("zip",   "").strip()
            using_billing = True

        # ── No usable address at all ──────────────────────────────────────────
        if not street and not city:
            self._ops.append(UpdateOne(
                {"_id": provider["_id"]},
                {"$set": {"county": {"fips": None, "source": "geocoder_no_address"}}}
            ))
            self._no_address += 1
            return

        # ── Geocoder call ─────────────────────────────────────────────────────
        result = _geocode_address(street=street, city=city, state=state, zip_code=zip_code)
        time.sleep(0.05)  # 20 req/sec — Census Geocoder rate limit
        if result:
            source = "geocoder_pass2_billing" if using_billing else "geocoder_pass2"
            self._ops.append(UpdateOne(
                {"_id": provider["_id"]},
                {"$set": {"county": {"fips": result["fips"], "name": result["name"], "source": source}}}
            ))
            if using_billing:
                self._billing_modified += 1
            else:
                self._modified += 1
        else:
            # Geocoder returned no match — mark so Pass 2 never re-attempts this record
            self._ops.append(UpdateOne(
                {"_id": provider["_id"]},
                {"$set": {"county": {"fips": None, "source": "geocoder_failed"}}}
            ))
            self._geocoder_failed += 1

    def _pipeline_row_key(self) -> str:
        if 0 <= self._idx < len(self._providers):
            return str(self._providers[self._idx]["_id"])
        return f"provider_idx_{self._idx}"

    def _pipeline_resume(self) -> None:
        pass  # _pipeline_has_next() advances the cursor; no local state to reset

    def _pipeline_close(self) -> None:
        if self._ops and self._collection is not None:
            self._collection.bulk_write(self._ops, ordered=False)
            self._ops = []

    def _pipeline_build_result(self) -> dict:
        return {
            "modified": self._modified,
            "billing_modified": self._billing_modified,
            "geocoder_failed": self._geocoder_failed,
            "no_address": self._no_address,
            "started_at": self._started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(time.monotonic() - self._start_time, 2),
        }


def enrich_by_address_batch_fn(config: dict) -> dict:
    """Pass 2: Census Geocoder per provider. One updateOne per provider."""
    return AddressEnrichmentWorker(config).pipeline_execute()


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


