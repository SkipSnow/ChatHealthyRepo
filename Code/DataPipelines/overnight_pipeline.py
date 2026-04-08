# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Overnight Pipeline — runs quality enrichment on MS and VA,
# then copies all collections to frontend cluster.
#
# Standalone script — survives session disconnect.
# Run: python Code/DataPipelines/overnight_pipeline.py
#
# Steps:
#   1. can_prescribe on MS and VA
#   2. Part D drug load for MS and VA
#   3. Crosswalk enrichment for MS and VA
#   4. Specialty normalization for MS and VA
#   5. CopyToFrontEnd: providers for DE, MS, CA, VA
#   6. CopyToFrontEnd: SpecialtyMetaData
#   7. CopyToFrontEnd: provider_quality
#   8. Verify parity across all collections and states

import csv
import io
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

# Setup paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(SCRIPT_DIR, "..", ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(SCRIPT_DIR, "overnight_pipeline.log"), mode="w"),
    ],
)
log = logging.getLogger("overnight")

PIPELINE_URI = os.environ["MONGO_connectionString"]
FRONTEND_URI = os.environ["MONGO_FRONTEND_connectionString"]
STATES = ["MS", "VA"]
ALL_STATES = ["DE", "MS", "CA", "VA"]
ENV_PREFIX = os.environ.get("ENV_PREFIX", "dev")

from pymongo import MongoClient

pipeline_client = MongoClient(PIPELINE_URI)
frontend_client = MongoClient(FRONTEND_URI)


def step_1_can_prescribe():
    """Run mark_prescriber_fn on MS and VA."""
    log.info("=" * 60)
    log.info("STEP 1: can_prescribe on %s", STATES)
    log.info("=" * 60)
    from county_enrichment_job import mark_prescriber_fn
    for state in STATES:
        log.info("Processing %s...", state)
        result = mark_prescriber_fn({
            "provider_collection": "{ENV_PREFIX}_PublicHealthData.providers",
            "states": [state],
        })
        log.info("%s result: %s", state, result)


def step_2_part_d_drugs():
    """Load CMS Part D drugs for MS and VA."""
    log.info("=" * 60)
    log.info("STEP 2: Part D drug load for %s", STATES)
    log.info("=" * 60)
    import requests
    from prescriber_load_worker import _group_drugs
    from pymongo import UpdateOne

    url = "https://data.cms.gov/sites/default/files/2025-04/0d5915ce-002c-4d87-bde8-24ffb08bb6cc/MUP_DPR_RY25_P04_V10_DY23_NPIBN.csv"

    log.info("Downloading CMS Part D CSV...")
    resp = requests.get(url, stream=True, timeout=600)
    resp.raise_for_status()
    content = b""
    for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
        content += chunk
        if len(content) % (200 * 1024 * 1024) < 8 * 1024 * 1024:
            log.info("  Downloaded %d MB...", len(content) // (1024 * 1024))

    log.info("Download complete: %d MB. Parsing...", len(content) // (1024 * 1024))
    text = content.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    state_set = set(STATES)
    npis_by_state = {s: defaultdict(list) for s in STATES}

    for row in reader:
        state = (row.get("Prscrbr_State_Abrvtn") or "").strip().upper()
        if state not in state_set:
            continue
        npi = (row.get("Prscrbr_NPI") or "").strip()
        if not npi:
            continue
        brand = (row.get("Brnd_Name") or "").strip()
        generic = (row.get("Gnrc_Name") or "").strip()
        try:
            claims = int(row.get("Tot_Clms", 0))
        except (ValueError, TypeError):
            claims = 0
        try:
            benes = int(row.get("Tot_Benes", 0) or 0) if row.get("Tot_Benes", "").strip() not in ("", "*") else 0
        except (ValueError, TypeError):
            benes = 0
        npis_by_state[state][npi].append({
            "brand_name": brand,
            "generic_name": generic,
            "total_claims": claims,
            "total_beneficiaries": benes,
        })

    coll = pipeline_client[f"{ENV_PREFIX}_PublicHealthData"]["providers"]
    for state in STATES:
        npi_drugs = npis_by_state[state]
        log.info("%s: %d NPIs with drug data", state, len(npi_drugs))
        batch = []
        written = 0
        for npi, raw_drugs in npi_drugs.items():
            drug_list, generic_ratio_band = _group_drugs(raw_drugs)
            batch.append(UpdateOne(
                {"npi": npi},
                {"$set": {
                    "can_prescribe.drugs": drug_list,
                    "can_prescribe.cost_measures.generic_ratio_band": generic_ratio_band,
                    "can_prescribe.data_year": 2023,
                    "can_prescribe.data_source": "CMS Part D Prescriber PUF RY25",
                    "can_prescribe.drugs_loaded_at": datetime.now(timezone.utc).isoformat(),
                }},
            ))
            written += 1
            if len(batch) >= 500:
                coll.bulk_write(batch, ordered=False)
                log.info("  %s: written %d NPIs", state, written)
                batch = []
        if batch:
            coll.bulk_write(batch, ordered=False)
        log.info("%s: Part D complete — %d NPIs enriched", state, written)


def step_3_crosswalk():
    """Run crosswalk enrichment for MS and VA."""
    log.info("=" * 60)
    log.info("STEP 3: Crosswalk enrichment for %s", STATES)
    log.info("=" * 60)
    from crosswalk_builder import enrich_providers_with_crosswalk
    for state in STATES:
        log.info("Crosswalk for %s...", state)
        result = enrich_providers_with_crosswalk(env_prefix="dev", states=[state])
        log.info("%s result: %s", state, result)


def step_4_specialty_normalization():
    """Run specialty normalization for MS and VA."""
    log.info("=" * 60)
    log.info("STEP 4: Specialty normalization for %s", STATES)
    log.info("=" * 60)
    from crosswalk_builder import compute_specialty_baselines
    for state in STATES:
        log.info("Specialty normalization for %s...", state)
        result = compute_specialty_baselines(env_prefix="dev", states=[state])
        log.info("%s result: %s", state, result)


def step_5_copy_providers():
    """Copy providers from pipeline to frontend for all states."""
    log.info("=" * 60)
    log.info("STEP 5: CopyToFrontEnd — providers for %s", ALL_STATES)
    log.info("=" * 60)
    src = pipeline_client[f"{ENV_PREFIX}_PublicHealthData"]["providers"]
    dst = frontend_client[f"{ENV_PREFIX}_PublicHealthData"]["providers"]

    # Drop and re-insert
    dst.drop()
    log.info("Dropped frontend providers collection")

    total = 0
    batch = []
    cursor = src.find({})
    for doc in cursor:
        doc.pop("_id", None)
        batch.append(doc)
        if len(batch) >= 5000:
            dst.insert_many(batch)
            total += len(batch)
            log.info("  Copied %d providers...", total)
            batch = []
    if batch:
        dst.insert_many(batch)
        total += len(batch)

    log.info("Providers copy complete: %d records", total)


def step_6_copy_specialty():
    """Copy SpecialtyMetaData from pipeline to frontend."""
    log.info("=" * 60)
    log.info("STEP 6: CopyToFrontEnd — SpecialtyMetaData")
    log.info("=" * 60)
    src = pipeline_client[f"{ENV_PREFIX}_PublicHealthData"]["SpecialtyMetaData"]
    dst = frontend_client[f"{ENV_PREFIX}_PublicHealthData"]["SpecialtyMetaData"]

    dst.drop()
    docs = list(src.find({}))
    for doc in docs:
        doc.pop("_id", None)
    dst.insert_many(docs)
    log.info("SpecialtyMetaData copy complete: %d records", len(docs))


def step_7_copy_quality():
    """Copy provider_quality from pipeline to frontend."""
    log.info("=" * 60)
    log.info("STEP 7: CopyToFrontEnd — provider_quality")
    log.info("=" * 60)
    src = pipeline_client[f"{ENV_PREFIX}_PublicHealthData"]["provider_quality"]
    dst = frontend_client[f"{ENV_PREFIX}_PublicHealthData"]["provider_quality"]

    src_count = src.count_documents({})
    if src_count == 0:
        log.info("No provider_quality records on pipeline — skipping")
        return

    dst.drop()
    total = 0
    batch = []
    for doc in src.find({}):
        doc.pop("_id", None)
        batch.append(doc)
        if len(batch) >= 5000:
            dst.insert_many(batch)
            total += len(batch)
            log.info("  Copied %d quality records...", total)
            batch = []
    if batch:
        dst.insert_many(batch)
        total += len(batch)
    log.info("provider_quality copy complete: %d records", total)


def step_8_verify_parity():
    """Verify all collections match between pipeline and frontend."""
    log.info("=" * 60)
    log.info("STEP 8: Parity verification")
    log.info("=" * 60)
    all_pass = True

    # Providers by state
    src_coll = pipeline_client[f"{ENV_PREFIX}_PublicHealthData"]["providers"]
    dst_coll = frontend_client[f"{ENV_PREFIX}_PublicHealthData"]["providers"]
    for state in ALL_STATES:
        src_count = src_coll.count_documents({"practice_address.state": state})
        dst_count = dst_coll.count_documents({"practice_address.state": state})
        match = "PASS" if src_count == dst_count else "FAIL"
        if match == "FAIL":
            all_pass = False
        log.info("  Providers %s: pipeline=%d frontend=%d %s", state, src_count, dst_count, match)

    # SpecialtyMetaData
    src_sm = pipeline_client[f"{ENV_PREFIX}_PublicHealthData"]["SpecialtyMetaData"].count_documents({})
    dst_sm = frontend_client[f"{ENV_PREFIX}_PublicHealthData"]["SpecialtyMetaData"].count_documents({})
    match = "PASS" if src_sm == dst_sm else "FAIL"
    if match == "FAIL":
        all_pass = False
    log.info("  SpecialtyMetaData: pipeline=%d frontend=%d %s", src_sm, dst_sm, match)

    # provider_quality
    src_pq = pipeline_client[f"{ENV_PREFIX}_PublicHealthData"]["provider_quality"].count_documents({})
    dst_pq = frontend_client[f"{ENV_PREFIX}_PublicHealthData"]["provider_quality"].count_documents({})
    match = "PASS" if src_pq == dst_pq else "FAIL"
    if match == "FAIL":
        all_pass = False
    log.info("  provider_quality: pipeline=%d frontend=%d %s", src_pq, dst_pq, match)

    # can_prescribe flags
    for state in ALL_STATES:
        has_flag = dst_coll.count_documents({"practice_address.state": state, "can_prescribe": {"$exists": True}})
        total = dst_coll.count_documents({"practice_address.state": state})
        pct = (has_flag / total * 100) if total > 0 else 0
        log.info("  can_prescribe %s: %d/%d (%.0f%%)", state, has_flag, total, pct)

    if all_pass:
        log.info("PARITY: ALL PASS")
    else:
        log.error("PARITY: FAILURES DETECTED")

    return all_pass


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("OVERNIGHT PIPELINE — %s", datetime.now(timezone.utc).isoformat())
    log.info("States to enrich: %s", STATES)
    log.info("States to copy: %s", ALL_STATES)
    log.info("=" * 60)

    start = time.time()

    try:
        step_1_can_prescribe()
        step_2_part_d_drugs()
        step_3_crosswalk()
        step_4_specialty_normalization()
        step_5_copy_providers()
        step_6_copy_specialty()
        step_7_copy_quality()
        parity_ok = step_8_verify_parity()
    except Exception as e:
        log.error("PIPELINE FAILED: %s", e, exc_info=True)
        parity_ok = False

    elapsed = time.time() - start
    log.info("=" * 60)
    log.info("OVERNIGHT PIPELINE COMPLETE — %.1f minutes", elapsed / 60)
    log.info("Parity: %s", "ALL PASS" if parity_ok else "FAILURES")
    log.info("=" * 60)
