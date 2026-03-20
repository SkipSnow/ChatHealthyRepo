# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""Provider Load Validator — automated spot-check against NPPES NPI Registry API.

Samples 3 providers per worker from the most recent load, looks each up on the
public NPPES API, and compares name, practice address, and taxonomy code.
Also verifies county enrichment is present.

Usage:
    python validate_provider_load.py [--load-id <id>] [--samples-per-worker <n>]

Output: summary table + JSON report written to admin.ValidationReport.
"""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(Path(__file__).parent.parent / ".env")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

NPPES_API_URL = "https://npiregistry.cms.hhs.gov/api/"
STAGING_COLLECTION = "PublicHealthData.providers_staging"
METADATA_COLLECTION = "admin.DataLoadMetadata"
REPORT_COLLECTION = "admin.ValidationReport"

# Fields to compare between our doc and NPPES API response
PASS_EMOJI = "PASS"
FAIL_EMOJI = "FAIL"
SKIP_EMOJI = "SKIP"  # provider not found in NPPES (deactivated/test NPI)


def _get_latest_load_id(client) -> str:
    """Return the most recent load_id from DataLoadMetadata."""
    db_name, coll_name = METADATA_COLLECTION.split(".", 1)
    doc = client[db_name][coll_name].find_one(
        {"type": "ProviderData"},
        sort=[("date", -1)],
    )
    if not doc:
        raise RuntimeError("No load metadata found — has LoadProviderData been run?")
    return doc["load_id"]


PROJECTION = {
    "_id": 0,
    "npi": 1,
    "worker_id": 1,
    "provider_last_name_legal_name": 1,
    "provider_first_name": 1,
    "provider_organization_name_legal_business_name": 1,
    "entity_type_code": 1,
    "practice_address": 1,
    "taxonomy_codes": 1,
    "county_fips": 1,
    "county_name": 1,
    "county_source": 1,
}


def _sample_providers(client, load_id: str, samples_per_worker: int) -> list:
    """Return providers sampled from each worker, split evenly between
    individuals (entity_type_code=1) and facilities (entity_type_code=2).
    """
    db_name, coll_name = STAGING_COLLECTION.split(".", 1)
    coll = client[db_name][coll_name]

    worker_ids = coll.distinct("worker_id", {"load_id": load_id})
    logging.info("Found %d workers for load_id %s", len(worker_ids), load_id)

    # Split samples_per_worker evenly: half individuals, half facilities.
    # Odd number goes to individuals (more common in NPI data).
    n_facilities = samples_per_worker // 2
    n_individuals = samples_per_worker - n_facilities

    samples = []
    for worker_id in sorted(worker_ids):
        base_filter = {"load_id": load_id, "worker_id": worker_id}

        individuals = list(coll.find(
            {**base_filter, "entity_type_code": "1"},
            PROJECTION,
            limit=n_individuals,
        ))
        facilities = list(coll.find(
            {**base_filter, "entity_type_code": "2"},
            PROJECTION,
            limit=n_facilities,
        ))
        samples.extend(individuals)
        samples.extend(facilities)

    individuals_total = sum(1 for s in samples if s.get("entity_type_code") == "1")
    facilities_total = sum(1 for s in samples if s.get("entity_type_code") == "2")
    logging.info(
        "Sampled %d providers across %d workers (%d individuals, %d facilities)",
        len(samples), len(worker_ids), individuals_total, facilities_total,
    )
    return samples


def _lookup_nppes(npi: str) -> dict | None:
    """Call NPPES API. Returns parsed result or None if not found."""
    try:
        resp = requests.get(
            NPPES_API_URL,
            params={"number": npi, "version": "2.1"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        return results[0] if results else None
    except Exception as exc:
        logging.warning("NPPES lookup failed for NPI %s: %s", npi, exc)
        return None


def _compare(our_doc: dict, nppes: dict) -> dict:
    """Compare our stored fields against NPPES API response. Returns comparison dict."""
    checks = {}

    # ── Name ──────────────────────────────────────────────────────────────────
    entity_type = our_doc.get("entity_type_code", "1")
    if entity_type == "2":
        # Organization
        our_name = (our_doc.get("provider_organization_name_legal_business_name") or "").upper().strip()
        nppes_name = (nppes.get("basic", {}).get("organization_name") or "").upper().strip()
    else:
        # Individual — last name
        our_name = (our_doc.get("provider_last_name_legal_name") or "").upper().strip()
        nppes_name = (nppes.get("basic", {}).get("last_name") or "").upper().strip()

    checks["name"] = {
        "result": PASS_EMOJI if our_name == nppes_name else FAIL_EMOJI,
        "ours": our_name,
        "nppes": nppes_name,
    }

    # ── Practice address ───────────────────────────────────────────────────────
    nppes_location = next(
        (a for a in nppes.get("addresses", []) if a.get("address_purpose") == "LOCATION"),
        None,
    )
    our_addr = our_doc.get("practice_address", {})

    if nppes_location:
        # City
        our_city = (our_addr.get("city") or "").upper().strip()
        nppes_city = (nppes_location.get("city") or "").upper().strip()
        checks["city"] = {
            "result": PASS_EMOJI if our_city == nppes_city else FAIL_EMOJI,
            "ours": our_city,
            "nppes": nppes_city,
        }

        # State
        our_state = (our_addr.get("state") or "").upper().strip()
        nppes_state = (nppes_location.get("state") or "").upper().strip()
        checks["state"] = {
            "result": PASS_EMOJI if our_state == nppes_state else FAIL_EMOJI,
            "ours": our_state,
            "nppes": nppes_state,
        }

        # ZIP — compare first 5 digits only
        our_zip = (our_addr.get("zip") or "")[:5]
        nppes_zip = (nppes_location.get("postal_code") or "")[:5]
        checks["zip"] = {
            "result": PASS_EMOJI if our_zip == nppes_zip else FAIL_EMOJI,
            "ours": our_zip,
            "nppes": nppes_zip,
        }
    else:
        checks["city"] = checks["state"] = checks["zip"] = {
            "result": SKIP_EMOJI, "ours": "", "nppes": "no LOCATION address in NPPES"
        }

    # ── Taxonomy ───────────────────────────────────────────────────────────────
    our_tax = (our_doc.get("taxonomy_codes") or [""])[0]
    nppes_tax = next(
        (t.get("code", "") for t in nppes.get("taxonomies", []) if t.get("primary")),
        (nppes.get("taxonomies") or [{}])[0].get("code", "") if nppes.get("taxonomies") else "",
    )
    checks["taxonomy"] = {
        "result": PASS_EMOJI if our_tax == nppes_tax else FAIL_EMOJI,
        "ours": our_tax,
        "nppes": nppes_tax,
    }

    # ── County enrichment ─────────────────────────────────────────────────────
    has_county = bool(our_doc.get("county_fips"))
    checks["county_enriched"] = {
        "result": PASS_EMOJI if has_county else FAIL_EMOJI,
        "county_fips": our_doc.get("county_fips", "MISSING"),
        "county_name": our_doc.get("county_name", ""),
        "county_source": our_doc.get("county_source", ""),
    }

    return checks


def validate(load_id: str = None, samples_per_worker: int = 3) -> dict:
    client = MongoClient(os.environ["MONGO_connectionString"])
    try:
        if not load_id:
            load_id = _get_latest_load_id(client)
            logging.info("Using latest load_id: %s", load_id)

        providers = _sample_providers(client, load_id, samples_per_worker)

        results = []
        counters = {"pass": 0, "fail": 0, "skip": 0, "not_found": 0}

        for i, doc in enumerate(providers):
            npi = doc.get("npi")
            if not npi:
                logging.warning("Provider missing NPI — skipping")
                continue

            nppes = _lookup_nppes(npi)
            time.sleep(0.1)  # be polite to NPPES API

            if nppes is None:
                counters["not_found"] += 1
                results.append({
                    "npi": npi,
                    "worker_id": doc.get("worker_id"),
                    "result": "NOT_FOUND",
                    "checks": {},
                })
                logging.info("[%d/%d] NPI %s — NOT FOUND in NPPES", i + 1, len(providers), npi)
                continue

            checks = _compare(doc, nppes)

            # Overall result: FAIL if any check failed
            failed_checks = [k for k, v in checks.items() if v["result"] == FAIL_EMOJI]
            overall = FAIL_EMOJI if failed_checks else PASS_EMOJI
            counters["fail" if failed_checks else "pass"] += 1

            results.append({
                "npi": npi,
                "worker_id": doc.get("worker_id"),
                "result": overall,
                "failed_checks": failed_checks,
                "checks": checks,
            })

            status_str = f"{overall}" + (f" [{', '.join(failed_checks)}]" if failed_checks else "")
            logging.info("[%d/%d] NPI %s — worker %s — %s",
                         i + 1, len(providers), npi, doc.get("worker_id"), status_str)

        summary = {
            "load_id": load_id,
            "datetime": datetime.now(timezone.utc).isoformat(),
            "total_sampled": len(results),
            "pass": counters["pass"],
            "fail": counters["fail"],
            "not_found_in_nppes": counters["not_found"],
            "pass_rate": f"{counters['pass'] / len(results) * 100:.1f}%" if results else "N/A",
            "results": results,
        }

        # Write report to MongoDB
        db_name, coll_name = REPORT_COLLECTION.split(".", 1)
        client[db_name][coll_name].insert_one({**summary})
        logging.info("Validation report written to %s", REPORT_COLLECTION)

        # Print summary table
        print("\n" + "=" * 60)
        print(f"VALIDATION SUMMARY  —  load_id: {load_id}")
        print("=" * 60)
        print(f"  Sampled:      {summary['total_sampled']}")
        print(f"  PASS:         {summary['pass']}")
        print(f"  FAIL:         {summary['fail']}")
        print(f"  Not in NPPES: {summary['not_found_in_nppes']}")
        print(f"  Pass rate:    {summary['pass_rate']}")
        print("=" * 60)

        failures = [r for r in results if r["result"] == FAIL_EMOJI]
        if failures:
            print(f"\nFAILURES ({len(failures)}):")
            for f in failures[:20]:
                print(f"  NPI {f['npi']} (worker {f['worker_id']}) — {f['failed_checks']}")
                for check_name in f["failed_checks"]:
                    c = f["checks"][check_name]
                    print(f"    {check_name}: ours={c.get('ours')} nppes={c.get('nppes')}")

        return summary

    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate provider load against NPPES API")
    parser.add_argument("--load-id", help="Specific load_id to validate (default: latest)")
    parser.add_argument("--samples-per-worker", type=int, default=3,
                        help="Providers to sample per worker (default: 3)")
    args = parser.parse_args()

    result = validate(load_id=args.load_id, samples_per_worker=args.samples_per_worker)
    print(json.dumps(result["results"][:5], indent=2, default=str))
