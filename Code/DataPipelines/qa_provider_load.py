# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""QA script for providers_staging — run before deploying to ChatHealthyFrontEnd.

Checks:
  1. Record count     — total docs within expected range (±5% of last known good)
  2. Enrichment ratio — ≥ ENRICHMENT_THRESHOLD of addressable providers have county.fips
  3. Spot check       — SAMPLES_PER_PARTITION random samples from each of NUM_PARTITIONS
                        NPI-range partitions; validates field presence, source enum,
                        FIPS format, and state-FIPS agreement

Usage:
  python qa_provider_load.py [--collection PublicHealthData.providers_staging]
                             [--workers 32] [--samples 2] [--threshold 0.95]

Exit code: 0 = all gates passed, 1 = one or more gates failed.
"""

import argparse
import json
import logging
import os
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

# ── Configuration ──────────────────────────────────────────────────────────────

DEFAULT_COLLECTION     = "dev_PublicHealthData.providers"
EXPECTED_MIN           = 8_500_000   # hard lower bound — fail if below this
EXPECTED_MAX           = 10_500_000  # hard upper bound — fail if above this
EXPECTED_NOMINAL       = 8_900_000   # last known good; warn if >5% deviation
ENRICHMENT_THRESHOLD   = 0.98        # 98% of in-scope providers must have county.fips
NUM_PARTITIONS         = 32          # mirrors num_workers used in the load job
SAMPLES_PER_PARTITION  = 2           # 2–3 per partition; 2 is the default

VALID_SOURCES = {
    "crosswalk_pass1",
    "geocoder_practice",
    "geocoder_mailing",
    "geocoder_pass3_billing",
    "geocoder_failed",
    "geocoder_no_address",
    "out_of_scope",
}

# FIPS 2-digit state prefix → 2-letter abbreviation (US states + DC + PR)
FIPS_STATE_PREFIX: dict[str, str] = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA",
    "08": "CO", "09": "CT", "10": "DE", "11": "DC", "12": "FL",
    "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN",
    "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME",
    "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS",
    "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI",
    "56": "WY", "72": "PR", "78": "VI",
}

# ── MongoDB ─────────────────────────────────────────────────────────────────────

_mongo: MongoClient | None = None


def _get_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(os.environ["MONGO_connectionString"])
    return _mongo


# ── Validation helpers ──────────────────────────────────────────────────────────

def _validate_doc(doc: dict) -> list[str]:
    """Return list of validation error strings for a single provider document."""
    errors = []
    npi = doc.get("npi", "")

    # NPI: 10-digit string
    if not npi or not re.fullmatch(r"\d{10}", str(npi)):
        errors.append(f"npi invalid or missing: {npi!r}")

    # Name: at least one of individual or org name
    last  = doc.get("provider_last_name_legal_name", "")
    org   = doc.get("provider_organization_name_legal_business_name", "")
    if not (last or org):
        errors.append("missing both provider_last_name and org_name")

    # County sub-document
    county = doc.get("county") or {}
    source = county.get("source")
    fips   = county.get("fips")

    if source not in VALID_SOURCES:
        errors.append(f"county.source is unexpected: {source!r}")
        return errors   # remaining checks don't make sense without a valid source

    if source == "out_of_scope":
        return errors   # inactive/foreign — no further enrichment expected

    # Enriched sources must have a valid fips
    enriched_sources = {
        "crosswalk_pass1", "geocoder_practice",
        "geocoder_mailing", "geocoder_pass3_billing",
    }
    if source in enriched_sources:
        if not fips:
            errors.append(f"county.fips is null for source={source!r}")
        elif not re.fullmatch(r"\d{5}", str(fips)):
            errors.append(f"county.fips format invalid: {fips!r} (expected 5-digit FIPS)")
        else:
            # Cross-check: FIPS state prefix must match provider's practice state
            state_prefix = str(fips)[:2]
            fips_state   = FIPS_STATE_PREFIX.get(state_prefix)
            doc_state    = (doc.get("practice_address") or {}).get("state", "")
            if fips_state and doc_state and fips_state != doc_state.upper():
                errors.append(
                    f"FIPS state mismatch: county.fips {fips!r} → {fips_state}, "
                    f"but practice_address.state={doc_state!r}"
                )

    return errors


# ── Gate 1: Record count ────────────────────────────────────────────────────────

def check_record_count(coll) -> dict:
    total = coll.count_documents({})
    nominal_dev = abs(total - EXPECTED_NOMINAL) / EXPECTED_NOMINAL
    return {
        "gate":        "record_count",
        "total":       total,
        "expected_min": EXPECTED_MIN,
        "expected_max": EXPECTED_MAX,
        "expected_nominal": EXPECTED_NOMINAL,
        "deviation_pct": round(nominal_dev * 100, 1),
        "passed": EXPECTED_MIN <= total <= EXPECTED_MAX,
        "warn":   nominal_dev > 0.05,  # warn if >5% from last known good
    }


# ── Gate 2: Enrichment ratio ────────────────────────────────────────────────────

def check_enrichment_ratio(coll, threshold: float) -> dict:
    out_of_scope = coll.count_documents({"county.source": "out_of_scope"})
    total        = coll.count_documents({})
    addressable  = total - out_of_scope
    enriched     = coll.count_documents({
        "county.fips":   {"$ne": None},
        "county.source": {"$nin": ["out_of_scope"]},
    })
    failed       = coll.count_documents({"county.source": "geocoder_failed"})
    no_address   = coll.count_documents({"county.source": "geocoder_no_address"})

    ratio = enriched / addressable if addressable else 0.0

    pipeline = [
        {"$match": {"county.source": {"$ne": "out_of_scope"}}},
        {"$group": {
            "_id":      "$practice_address.state",
            "total":    {"$sum": 1},
            "enriched": {"$sum": {"$cond": [{"$ne": ["$county.fips", None]}, 1, 0]}},
        }},
        {"$addFields": {
            "pct": {"$round": [{"$multiply": [{"$divide": ["$enriched", "$total"]}, 100]}, 1]}
        }},
        {"$sort": {"pct": 1}},
        {"$limit": 10},   # bottom 10 states
    ]
    worst_states = list(coll.aggregate(pipeline))

    return {
        "gate":         "enrichment_ratio",
        "total":        total,
        "out_of_scope": out_of_scope,
        "addressable":  addressable,
        "enriched":     enriched,
        "failed":       failed,
        "no_address":   no_address,
        "ratio":        round(ratio, 4),
        "threshold":    threshold,
        "passed":       ratio >= threshold,
        "worst_states": worst_states,
    }


# ── Gate 3: Spot check ─────────────────────────────────────────────────────────

def check_spot(coll, num_partitions: int, samples_per_partition: int) -> dict:
    """Sample randomly from each of num_partitions NPI-range partitions.

    NPI is a 10-digit string. We partition the [1000000000, 2999999999] range
    (CMS-issued NPIs) into equal-width buckets and sample from each bucket.
    Buckets with no documents are skipped.
    """
    NPI_MIN = 1_000_000_000
    NPI_MAX = 2_999_999_999
    bucket_width = (NPI_MAX - NPI_MIN) // num_partitions

    errors_by_npi: dict[str, list[str]] = {}
    sampled = 0
    empty_partitions = 0

    for i in range(num_partitions):
        lo = NPI_MIN + i * bucket_width
        hi = lo + bucket_width - 1
        lo_str = str(lo)
        hi_str = str(hi)

        docs = list(coll.aggregate([
            {"$match": {"npi": {"$gte": lo_str, "$lte": hi_str}}},
            {"$sample": {"size": samples_per_partition}},
            {"$project": {
                "npi": 1,
                "provider_last_name_legal_name": 1,
                "provider_organization_name_legal_business_name": 1,
                "practice_address": 1,
                "county": 1,
                "_id": 0,
            }},
        ]))

        if not docs:
            empty_partitions += 1
            continue

        for doc in docs:
            sampled += 1
            doc_errors = _validate_doc(doc)
            if doc_errors:
                errors_by_npi[doc.get("npi", "unknown")] = doc_errors

    error_rate = len(errors_by_npi) / sampled if sampled else 0.0

    return {
        "gate":              "spot_check",
        "partitions":        num_partitions,
        "samples_requested": num_partitions * samples_per_partition,
        "sampled":           sampled,
        "empty_partitions":  empty_partitions,
        "errors":            errors_by_npi,
        "error_count":       len(errors_by_npi),
        "error_rate":        round(error_rate, 4),
        "passed":            len(errors_by_npi) == 0,
    }


# ── Report ──────────────────────────────────────────────────────────────────────

def _print_report(results: list[dict]) -> None:
    WIDTH = 64
    print("=" * WIDTH)
    print("  providers_staging QA Report")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * WIDTH)

    overall = all(r["passed"] for r in results)

    for r in results:
        gate   = r["gate"]
        passed = r["passed"]
        icon   = "PASS" if passed else "FAIL"
        print(f"\n[{icon}] {gate}")

        if gate == "record_count":
            print(f"  Total records : {r['total']:,}")
            print(f"  Expected range: {r['expected_min']:,} – {r['expected_max']:,}")
            print(f"  Nominal       : {r['expected_nominal']:,} (deviation {r['deviation_pct']}%)")
            if r["warn"]:
                print(f"  WARN: deviation > 5% from last known good — verify source file")

        elif gate == "enrichment_ratio":
            pct = r["ratio"] * 100
            print(f"  Addressable   : {r['addressable']:,}")
            print(f"  Enriched      : {r['enriched']:,}  ({pct:.1f}%  threshold {r['threshold']*100:.0f}%)")
            print(f"  Out of scope  : {r['out_of_scope']:,}")
            print(f"  Failed        : {r['failed']:,}  (geocoder_failed)")
            print(f"  No address    : {r['no_address']:,}  (geocoder_no_address)")
            if r["worst_states"]:
                print(f"  Worst states (bottom {len(r['worst_states'])}):")
                for s in r["worst_states"]:
                    print(f"    {s['_id'] or '??':5s}  {s['pct']}%  ({s['enriched']:,}/{s['total']:,})")

        elif gate == "spot_check":
            print(f"  Partitions    : {r['partitions']}   ({r['samples_requested']} requested, {r['sampled']} sampled)")
            if r["empty_partitions"]:
                print(f"  Empty buckets : {r['empty_partitions']}  (NPI ranges with no documents)")
            print(f"  Errors        : {r['error_count']}  (error rate {r['error_rate']*100:.1f}%)")
            for npi, errs in r["errors"].items():
                for e in errs:
                    print(f"    NPI {npi}: {e}")

    print()
    print("=" * WIDTH)
    print(f"  OVERALL: {'PASS — safe to deploy' if overall else 'FAIL — do not deploy'}")
    print("=" * WIDTH)


# ── Main ────────────────────────────────────────────────────────────────────────

def run_qa(
    collection: str = DEFAULT_COLLECTION,
    num_workers: int = NUM_PARTITIONS,
    samples_per_partition: int = SAMPLES_PER_PARTITION,
    threshold: float = ENRICHMENT_THRESHOLD,
) -> list[dict]:
    db_name, coll_name = collection.split(".", 1)
    coll = _get_client()[db_name][coll_name]

    logging.info("Running QA on %s", collection)
    results = [
        check_record_count(coll),
        check_enrichment_ratio(coll, threshold),
        check_spot(coll, num_workers, samples_per_partition),
    ]
    return results


if __name__ == "__main__":
    load_dotenv(Path(__file__).parent.parent / ".env")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="QA check for providers_staging")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--workers",    type=int, default=NUM_PARTITIONS,
                        help="Number of NPI-range partitions to sample from")
    parser.add_argument("--samples",    type=int, default=SAMPLES_PER_PARTITION,
                        help="Random samples per partition (default 2)")
    parser.add_argument("--threshold",  type=float, default=ENRICHMENT_THRESHOLD,
                        help="Minimum enrichment ratio (default 0.95)")
    parser.add_argument("--json",       action="store_true",
                        help="Output raw JSON instead of formatted report")
    args = parser.parse_args()

    results = run_qa(
        collection=args.collection,
        num_workers=args.workers,
        samples_per_partition=args.samples,
        threshold=args.threshold,
    )

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        _print_report(results)

    sys.exit(0 if all(r["passed"] for r in results) else 1)
