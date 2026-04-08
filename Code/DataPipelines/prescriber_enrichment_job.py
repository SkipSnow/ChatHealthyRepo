# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Prescriber Enrichment Job — enriches provider_quality in one pass per NPI.
# All enrichments in one step:
#   1. Drug → indication → ICD-10 mapping (LLM + cache)
#   2. OIG LEIE exclusion flag
#   3. SAM.gov exclusion flag
#   4. Location denormalization (from provider record)
#   5. Taxonomy + enumeration date (from provider record)

import csv
import io
import json
import logging
import os
from datetime import datetime, timezone

from pymongo import UpdateOne
from blob_client import get_blob_service
from pipeline_db import get_db

_log = logging.getLogger("prescriber_enrichment")


# ── Drug Indication Cache ──────────────────────────────────────────────────

def _get_cached_indication(env_prefix, drug_name):
    """Check drug_indication_cache for existing mapping."""
    coll = get_db(env_prefix)["drug_indication_cache"]
    return coll.find_one({"drug_name": drug_name.lower()}, {"_id": 0})


def _cache_indication(env_prefix, drug_name, mapping):
    """Store drug → indication → ICD-10 mapping in cache."""
    coll = get_db(env_prefix)["drug_indication_cache"]
    coll.update_one(
        {"drug_name": drug_name.lower()},
        {"$set": {
            "drug_name": drug_name.lower(),
            "mapping": mapping,
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )


def _llm_map_drug(drug_name):
    """Call GPT-4.1-mini to map drug → on-label indications → ICD-10 codes.
    Alpha path per design_eval_p_rx_drug_mapping in design.json."""
    from openai import OpenAI

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    prompt = f"""Map this drug to its FDA-approved on-label indications with ICD-10-CM codes.
Drug: {drug_name}

Return JSON only:
{{
  "indications": [
    {{
      "indication": "clinical description",
      "icd10_codes": ["code1", "code2"]
    }}
  ]
}}

Rules:
- Only FDA-approved on-label uses
- ICD-10-CM codes must be valid
- If unknown drug, return {{"indications": []}}
"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            max_tokens=500,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a pharmaceutical reference. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
        )
        result = json.loads(resp.choices[0].message.content)
        return result.get("indications", [])
    except Exception as e:
        _log.warning("LLM drug mapping failed for %s: %s", drug_name, e)
        return []


# ── OIG LEIE Loader ───────────────────────────────────────────────────────

def _load_oig_exclusions(blob_name="oig_leie_latest.csv", container="provider-data"):
    """Load OIG LEIE CSV from blob, return set of excluded NPIs."""
    try:
        blob_service = get_blob_service()
        blob_client = blob_service.get_blob_client(container, blob_name)
        stream = blob_client.download_blob()
        chunks = []
        for chunk in stream.chunks():
            chunks.append(chunk)
        content = b"".join(chunks).decode("utf-8", errors="replace")

        excluded_npis = set()
        reader = csv.DictReader(io.StringIO(content))
        for row in reader:
            npi = (row.get("NPI") or "").strip()
            if npi:
                excluded_npis.add(npi)

        _log.info("OIG LEIE loaded: %d excluded NPIs", len(excluded_npis))
        return excluded_npis
    except Exception as e:
        _log.warning("OIG LEIE load failed: %s — continuing without exclusion data", e)
        return set()


# ── SAM.gov Loader ────────────────────────────────────────────────────────

def _load_sam_exclusions(blob_name="sam_exclusion_latest.csv", container="provider-data"):
    """Load SAM.gov exclusion CSV from blob, return set of excluded NPIs."""
    try:
        blob_service = get_blob_service()
        blob_client = blob_service.get_blob_client(container, blob_name)
        stream = blob_client.download_blob()
        chunks = []
        for chunk in stream.chunks():
            chunks.append(chunk)
        content = b"".join(chunks).decode("utf-8", errors="replace")

        excluded_npis = set()
        reader = csv.DictReader(io.StringIO(content))
        for row in reader:
            # SAM uses various field names — check common ones
            npi = (row.get("NPI") or row.get("npi") or row.get("Unique Entity ID") or "").strip()
            if npi and npi.isdigit() and len(npi) == 10:
                excluded_npis.add(npi)

        _log.info("SAM exclusions loaded: %d excluded NPIs", len(excluded_npis))
        return excluded_npis
    except Exception as e:
        _log.warning("SAM exclusion load failed: %s — continuing without SAM data", e)
        return set()


# ── Main Enrichment ───────────────────────────────────────────────────────

def enrich_all(env_prefix: str = "dev", states: list = None, batch_size: int = 100):
    """Enrich all provider_quality records in one pass.

    For each NPI:
      1. Map drugs → indications → ICD-10 (LLM + cache)
      2. Set OIG exclusion flag
      3. Set SAM exclusion flag
      4. Copy location from provider record
      5. Copy taxonomy + enumeration date from provider record
    """
    states = states or ["DE"]
    db = get_db(env_prefix)
    quality_coll = db["provider_quality"]
    provider_coll = db["providers"]

    # Load exclusion sets once
    oig_excluded = _load_oig_exclusions()
    sam_excluded = _load_sam_exclusions()

    # Get all provider_quality NPIs
    total = quality_coll.count_documents({})
    _log.info("Enriching %d provider_quality records (states=%s)", total, states)

    cursor = quality_coll.find({}, {"npi": 1, "measures.prescriber_behavior.drugs": 1})
    batch = []
    provider_batch = []  # dual-write to providers.can_prescribe.drugs
    enriched = 0
    drugs_mapped = 0
    cache_hits = 0

    for doc in cursor:
        npi = doc["npi"]
        drugs = (doc.get("measures", {}).get("prescriber_behavior", {}).get("drugs") or [])

        # 1. Drug → indication → ICD-10
        for drug in drugs:
            molecule = drug.get("molecule", "")
            if not molecule:
                continue

            cached = _get_cached_indication(env_prefix, molecule)
            if cached:
                indications = cached.get("mapping", [])
                cache_hits += 1
            else:
                indications = _llm_map_drug(molecule)
                _cache_indication(env_prefix, molecule, indications)
                drugs_mapped += 1

            drug["indications"] = indications
            drug["icd10_codes"] = []
            for ind in indications:
                drug["icd10_codes"].extend(ind.get("icd10_codes", []))

        # 2. OIG exclusion
        oig_flag = npi in oig_excluded

        # 3. SAM exclusion
        sam_flag = npi in sam_excluded

        # 4 + 5. Location + taxonomy from provider record
        provider = provider_coll.find_one(
            {"npi": npi},
            {"practice_address": 1, "county": 1, "taxonomy_codes": 1,
             "enumeration_date": 1, "_id": 0}
        )

        update_fields = {
            "measures.prescriber_behavior.drugs": drugs,
            "measures.exclusion": {
                "oig_excluded": oig_flag,
                "sam_excluded": sam_flag,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
            "enriched_at": datetime.now(timezone.utc).isoformat(),
        }

        if provider:
            addr = provider.get("practice_address", {})
            county = provider.get("county", {})
            update_fields["location"] = {
                "state": addr.get("state", ""),
                "city": addr.get("city", ""),
                "zip": addr.get("zip", ""),
                "county": county.get("name", ""),
            }
            update_fields["measures.specialty_match"] = {
                "taxonomy_codes": provider.get("taxonomy_codes", []),
            }
            update_fields["measures.experience"] = {
                "enumeration_date": provider.get("enumeration_date", ""),
            }

        batch.append(UpdateOne({"npi": npi}, {"$set": update_fields}))

        # Dual-write: update providers.can_prescribe.drugs with indications
        # for FindCare filtering and embedding
        if drugs:
            provider_batch.append(UpdateOne(
                {"npi": npi},
                {"$set": {
                    "can_prescribe.drugs": drugs,
                    "can_prescribe.enriched_at": datetime.now(timezone.utc).isoformat(),
                }},
            ))

        enriched += 1

        if len(batch) >= batch_size:
            quality_coll.bulk_write(batch, ordered=False)
            if provider_batch:
                provider_coll.bulk_write(provider_batch, ordered=False)
                provider_batch = []
            _log.info("Enriched %d / %d (drugs mapped: %d, cache hits: %d)",
                      enriched, total, drugs_mapped, cache_hits)
            batch = []

    if batch:
        quality_coll.bulk_write(batch, ordered=False)
    if provider_batch:
        provider_coll.bulk_write(provider_batch, ordered=False)

    _log.info("Enrichment complete: %d NPIs enriched, %d drugs mapped, %d cache hits",
              enriched, drugs_mapped, cache_hits)

    return {
        "status": "complete",
        "npis_enriched": enriched,
        "drugs_mapped_by_llm": drugs_mapped,
        "drug_cache_hits": cache_hits,
        "oig_excluded_count": sum(1 for doc in quality_coll.find({"measures.exclusion.oig_excluded": True})),
        "sam_excluded_count": sum(1 for doc in quality_coll.find({"measures.exclusion.sam_excluded": True})),
    }
