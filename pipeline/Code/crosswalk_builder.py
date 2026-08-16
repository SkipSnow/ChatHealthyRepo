from chathealthy_lib.logging_service import ChatHealthyLoggingService
# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Crosswalk Builder — constructs molecule-to-indication-to-ICD10 crosswalk
# from public data sources (RxNorm, DrugCentral, UMLS).
#
# Replaces LLM-based drug mapping with deterministic, auditable lookups.
# Every lookup is cached in drug_crosswalk_cache on the pipeline cluster.
#
# Usage:
#   python crosswalk_builder.py --env dev --states DE

import argparse

import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from pymongo import UpdateOne

from pipeline_db import get_db

_log = ChatHealthyLoggingService()

CROSSWALK_VERSION = "0.1"

RXNORM_BASE = "https://rxnav.nlm.nih.gov/REST"

# Rate-limit: RxNorm asks for <= 20 requests/sec
_RXNORM_MIN_INTERVAL = 0.06  # ~16 req/sec with margin
_last_rxnorm_call = 0.0


# ── Helpers ───────────────────────────────────────────────────────────────


def _compute_band(pct: float) -> str:
    """Convert a percentage (0-100) to a 5% band string.

    Returns one of 20 bands: '0-5%', '5-10%', ... '95-100%'.
    """
    lower = int(pct // 5) * 5
    lower = min(lower, 95)  # cap at 95-100%
    return f"{lower}-{lower + 5}%"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── RxNorm Integration ────────────────────────────────────────────────────


def _rxnorm_throttle():
    """Enforce rate limit on RxNorm API calls."""
    global _last_rxnorm_call
    elapsed = time.time() - _last_rxnorm_call
    if elapsed < _RXNORM_MIN_INTERVAL:
        time.sleep(_RXNORM_MIN_INTERVAL - elapsed)
    _last_rxnorm_call = time.time()


def rxnorm_get_rxcui(drug_name: str) -> str | None:
    """Look up RXCUI for a drug name via RxNorm REST API.

    Endpoint: /rxcui.json?name={drugname}
    Returns the RXCUI string or None if not found.
    """
    _rxnorm_throttle()
    try:
        resp = requests.get(
            f"{RXNORM_BASE}/rxcui.json",
            params={"name": drug_name},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        id_group = data.get("idGroup", {})
        rxnorm_ids = id_group.get("rxnormId")
        if rxnorm_ids and len(rxnorm_ids) > 0:
            return rxnorm_ids[0]
    except Exception as e:
        _log.warning("RxNorm RXCUI lookup failed for '%s': %s", drug_name, e)
    return None


def rxnorm_get_ingredient(rxcui: str) -> dict | None:
    """Get the active ingredient (molecule) for an RXCUI.

    Endpoint: /rxcui/{rxcui}/related.json?tty=IN
    Returns {"rxcui": "...", "name": "..."} or None.
    """
    _rxnorm_throttle()
    try:
        resp = requests.get(
            f"{RXNORM_BASE}/rxcui/{rxcui}/related.json",
            params={"tty": "IN"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        groups = data.get("relatedGroup", {}).get("conceptGroup", [])
        for group in groups:
            if group.get("tty") == "IN":
                props = group.get("conceptProperties", [])
                if props:
                    return {
                        "rxcui": props[0].get("rxcui", ""),
                        "name": props[0].get("name", ""),
                    }
    except Exception as e:
        _log.warning("RxNorm ingredient lookup failed for RXCUI %s: %s", rxcui, e)
    return None


def rxnorm_normalize(drug_name: str) -> dict | None:
    """Normalize a drug name to its active ingredient via RxNorm.

    Returns {"rxcui": "...", "ingredient_rxcui": "...", "ingredient_name": "..."}
    or None if lookup fails.
    """
    rxcui = rxnorm_get_rxcui(drug_name)
    if not rxcui:
        # Try approximate match
        _rxnorm_throttle()
        try:
            resp = requests.get(
                f"{RXNORM_BASE}/approximateTerm.json",
                params={"term": drug_name, "maxEntries": 1},
                timeout=10,
            )
            resp.raise_for_status()
            candidates = resp.json().get("approximateGroup", {}).get("candidate", [])
            if candidates:
                rxcui = candidates[0].get("rxcui")
        except Exception as e:
            _log.warning("RxNorm approximate lookup failed for '%s': %s", drug_name, e)

    if not rxcui:
        return None

    ingredient = rxnorm_get_ingredient(rxcui)
    if ingredient:
        return {
            "rxcui": rxcui,
            "ingredient_rxcui": ingredient["rxcui"],
            "ingredient_name": ingredient["name"],
        }
    return {"rxcui": rxcui, "ingredient_rxcui": rxcui, "ingredient_name": drug_name}


# ── DrugCentral Integration ──────────────────────────────────────────────


def drugcentral_get_indications(molecule_name: str) -> list[dict]:
    """Look up indications for a molecule from DrugCentral.

    TODO: Wire to DrugCentral PostgreSQL dump when data download is complete.
    For alpha, returns empty list — RxClass is the primary source.
    """
    _log.debug("DrugCentral stub called for '%s' — no data yet", molecule_name)
    return []


# ── RxClass Integration (MED-RT may_treat) ─────────────────────────────
# Free API, no license needed. Uses VA's MED-RT drug→disease relationships.
# Returns MeSH disease codes which we map to ICD-10-CM.

RXCLASS_BASE = "https://rxnav.nlm.nih.gov/REST/rxclass"

# MeSH disease → ICD-10-CM mapping for common conditions.
# This is the proprietary crosswalk bridge — our secret sauce.
# Expanded as we encounter more diseases. UMLS will automate this when licensed.
_MESH_TO_ICD10: dict[str, list[str]] = {
    "Hypertension": ["I10"],
    "Heart Failure": ["I50.9"],
    "Diabetes Mellitus, Type 2": ["E11.9"],
    "Diabetes Mellitus, Type 1": ["E10.9"],
    "Diabetic Nephropathies": ["E11.22"],
    "Myocardial Infarction": ["I21.9"],
    "Ventricular Dysfunction, Left": ["I51.89"],
    "Hypercholesterolemia": ["E78.00"],
    "Hyperlipoproteinemias": ["E78.5"],
    "Hypertriglyceridemia": ["E78.1"],
    "Coronary Artery Disease": ["I25.10"],
    "Atrial Fibrillation": ["I48.91"],
    "Asthma": ["J45.909"],
    "Pulmonary Disease, Chronic Obstructive": ["J44.1"],
    "Depressive Disorder, Major": ["F33.9"],
    "Anxiety Disorders": ["F41.9"],
    "Pain": ["G89.29"],
    "Seizures": ["G40.909"],
    "Epilepsy": ["G40.909"],
    "Hypothyroidism": ["E03.9"],
    "Gastroesophageal Reflux": ["K21.0"],
    "Osteoporosis": ["M81.0"],
    "Rheumatoid Arthritis": ["M06.9"],
    "Gout": ["M10.9"],
    "Urinary Tract Infections": ["N39.0"],
    "Pneumonia": ["J18.9"],
    "Dementia": ["F03.90"],
    "Alzheimer Disease": ["G30.9"],
    "Parkinson Disease": ["G20"],
    "Stroke": ["I63.9"],
    "Prostatic Hyperplasia": ["N40.0"],
    "Bipolar Disorder": ["F31.9"],
    "Schizophrenia": ["F20.9"],
    "Attention Deficit Disorder with Hyperactivity": ["F90.9"],
    "Insomnia": ["G47.00"],
    "Migraine Disorders": ["G43.909"],
    "Obesity": ["E66.9"],
    "Anemia": ["D64.9"],
    "Thromboembolism": ["I82.90"],
    "Deep Vein Thrombosis": ["I82.40"],
    "Pulmonary Embolism": ["I26.99"],
    "Nausea": ["R11.0"],
    "Infections": ["B99.9"],
    "Neoplasms": ["C80.1"],
    "Edema": ["R60.9"],
    "Chronic Pain": ["G89.29"],
    "Glaucoma": ["H40.9"],
    "Psoriasis": ["L40.9"],
    "Dermatitis, Atopic": ["L20.9"],
    "HIV Infections": ["B20"],
    "Hepatitis C": ["B18.2"],
    "Kidney Failure, Chronic": ["N18.9"],
    "Epilepsies, Partial": ["G40.109"],
    "Phobic Disorders": ["F40.9"],
    "Neuralgia, Postherpetic": ["G53.0"],
    "Angina Pectoris, Variant": ["I20.1"],
    "Angina Pectoris": ["I20.9"],
    "Dyslipidemias": ["E78.5"],
    "Arteriosclerosis": ["I70.9"],
    "Peripheral Vascular Diseases": ["I73.9"],
    "Embolism and Thrombosis": ["I74.9"],
    "Arrhythmias, Cardiac": ["I49.9"],
    "Tachycardia": ["I47.9"],
    "Neuropathic Pain": ["G89.0"],
    "Fibromyalgia": ["M79.7"],
    "Restless Legs Syndrome": ["G25.81"],
    "Tremor": ["R25.1"],
    "Spasticity": ["R25.2"],
    "Inflammation": ["R68.89"],
    "Emesis": ["R11.10"],
    "Constipation": ["K59.00"],
    "Diarrhea": ["K59.1"],
    "Hyperlipidemia": ["E78.5"],
}


def rxclass_get_indications(molecule_name: str) -> list[dict]:
    """Look up indications via RxNorm RxClass API (MED-RT may_treat).

    Free API, no license beyond RxNorm (Category 0).
    Returns indications with ICD-10-CM codes from our proprietary bridge table.
    """
    _rxnorm_throttle()
    try:
        resp = requests.get(
            f"{RXCLASS_BASE}/class/byDrugName.json",
            params={
                "drugName": molecule_name,
                "relaSource": "MEDRT",
                "relas": "may_treat",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _log.warning("RxClass lookup failed for '%s': %s", molecule_name, e)
        return []

    if "rxclassDrugInfoList" not in data:
        return []

    seen = set()
    indications = []
    for item in data["rxclassDrugInfoList"]["rxclassDrugInfo"]:
        cls = item.get("rxclassMinConceptItem", {})
        name = cls.get("className", "")
        mesh_id = cls.get("classId", "")
        if not name or name in seen:
            continue
        seen.add(name)

        # Bridge MeSH disease name to ICD-10-CM via our proprietary table
        icd10_codes = _MESH_TO_ICD10.get(name, [])

        indications.append({
            "indication": name,
            "mesh_id": mesh_id,
            "icd10_cm": icd10_codes,
            "source": "rxclass_medrt",
        })

    return indications


# ── UMLS Integration ────────────────────────────────────────────────────
#
# UMLS license approved 2026-04-15. API key in .env as UMLS_API_KEY.
#
# UMLS bridges indication text (from drug labels) to ICD-10-CM codes via
# the UMLS Metathesaurus. Key APIs:
#   - /search/current?string={indication} — find CUI for indication text
#   - /content/current/CUI/{cui}/atoms?sabs=ICD10CM — get ICD-10-CM codes


def umls_get_icd10(indication_text: str, api_key: str = None) -> list[str]:
    """Map an indication string to ICD-10-CM codes via UMLS Metathesaurus.

    Uses the UMLS REST API with a valid API key (requires UMLS license).

    Args:
        indication_text: Clinical indication string (e.g. "Essential Hypertension")
        api_key: UMLS API key. Defaults to UMLS_API_KEY env var.

    Returns:
        List of ICD-10-CM codes (e.g. ["I10", "I11.9"])
    """
    api_key = api_key or os.environ.get("UMLS_API_KEY")
    if not api_key:
        _log.warning("UMLS_API_KEY not set — cannot map indications to ICD-10")
        return []

    base = "https://uts-ws.nlm.nih.gov/rest"

    # Step 1: Search for CUI
    try:
        resp = requests.get(
            f"{base}/search/current",
            params={"string": indication_text, "apiKey": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("result", {}).get("results", [])
        if not results:
            return []
        cui = results[0].get("ui", "")
    except Exception as e:
        _log.warning("UMLS search failed for '%s': %s", indication_text, e)
        return []

    # Step 2: Get ICD-10-CM atoms for this CUI
    try:
        resp = requests.get(
            f"{base}/content/current/CUI/{cui}/atoms",
            params={"sabs": "ICD10CM", "apiKey": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        atoms = resp.json().get("result", [])
        codes = []
        for atom in atoms:
            code = atom.get("code", "")
            # Extract code from URI if needed
            if "/" in code:
                code = code.rsplit("/", 1)[-1]
            if code:
                codes.append(code)
        return sorted(set(codes))
    except Exception as e:
        _log.warning("UMLS ICD-10 lookup failed for CUI %s: %s", cui, e)
        return []


# ── Crosswalk Cache ──────────────────────────────────────────────────────


def _get_cached_crosswalk(env_prefix: str, molecule_name: str) -> dict | None:
    """Check drug_crosswalk_cache for an existing crosswalk entry."""
    coll = get_db(env_prefix)["drug_crosswalk_cache"]
    return coll.find_one(
        {"molecule": molecule_name.lower()},
        {"_id": 0},
    )


def _cache_crosswalk(env_prefix: str, crosswalk: dict):
    """Store a crosswalk entry in drug_crosswalk_cache."""
    coll = get_db(env_prefix)["drug_crosswalk_cache"]
    coll.update_one(
        {"molecule": crosswalk["molecule"].lower()},
        {"$set": {
            "molecule": crosswalk["molecule"].lower(),
            "crosswalk": crosswalk,
            "cached_at": _now_iso(),
        }},
        upsert=True,
    )


# ── Main Crosswalk Function ─────────────────────────────────────────────


def build_crosswalk(molecule_name: str, env_prefix: str = "dev") -> dict:
    """Build a molecule-to-indication-to-ICD10 crosswalk for a single molecule.

    Lookup order:
      1. Check drug_crosswalk_cache (MongoDB)
      2. Normalize via RxNorm (brand/generic -> ingredient RXCUI)
      3. Look up indications via DrugCentral
      4. Bridge to ICD-10-CM via UMLS (licensed 2026-04-15)
      5. Cache the result

    Returns:
        {
            "molecule": "Lisinopril",
            "rxcui": "12345",
            "indications": [
                {
                    "indication": "Essential Hypertension",
                    "icd10_cm": ["I10"],
                    "source": "drugcentral"
                }
            ],
            "crosswalk_version": "0.1",
            "built_at": "2026-04-07T..."
        }
    """
    # 1. Check cache
    cached = _get_cached_crosswalk(env_prefix, molecule_name)
    if cached and cached.get("crosswalk"):
        _log.debug("Cache hit for '%s'", molecule_name)
        return cached["crosswalk"]

    _log.info("Building crosswalk for '%s'", molecule_name)

    # 2. Normalize via RxNorm
    rxcui = None
    ingredient_name = molecule_name
    rxnorm_result = rxnorm_normalize(molecule_name)
    if rxnorm_result:
        rxcui = rxnorm_result.get("ingredient_rxcui")
        ingredient_name = rxnorm_result.get("ingredient_name", molecule_name)
        _log.info("RxNorm: '%s' -> RXCUI %s (%s)", molecule_name, rxcui, ingredient_name)
    else:
        _log.warning("RxNorm: no match for '%s'", molecule_name)

    # 3. RxClass indications (primary source — free, no license)
    # Try ingredient name first, fall back to original input name
    indications = rxclass_get_indications(ingredient_name)
    if not indications and ingredient_name != molecule_name:
        indications = rxclass_get_indications(molecule_name)

    # 3b. DrugCentral indications (supplementary — when available)
    if not indications:
        indications = drugcentral_get_indications(ingredient_name)

    # 4. UMLS bridging — fill ICD-10 codes for indications
    # that RxClass returned without a match in _MESH_TO_ICD10.
    for ind in indications:
        if not ind.get("icd10_cm"):
            ind["icd10_cm"] = umls_get_icd10(ind["indication"])
            if ind["icd10_cm"]:
                ind["source"] = "umls"

    # 5. Build result
    crosswalk = {
        "molecule": ingredient_name,
        "molecule_input": molecule_name,
        "rxcui": rxcui,
        "indications": indications,
        "crosswalk_version": CROSSWALK_VERSION,
        "built_at": _now_iso(),
    }

    # 6. Cache
    _cache_crosswalk(env_prefix, crosswalk)

    return crosswalk


# ── Batch Enrichment ─────────────────────────────────────────────────────


def enrich_providers_with_crosswalk(
    env_prefix: str = "dev",
    states: list[str] | None = None,
    batch_size: int = 100,
):
    """Enrich all providers with crosswalk-based indication data.

    For each provider with can_prescribe.drugs:
      1. For each unique molecule, call build_crosswalk()
      2. Write indications back to can_prescribe.drugs[].indications
      3. Build the indication-first tree under can_prescribe.cost_measures.pathologies
      4. Compute 5% bands for cost_band and frequency_band per indication
      5. Compute provider-level on_label_band and generic_ratio_band
      6. Dual-write to provider_quality collection
    """
    states = states or ["DE"]
    db = get_db(env_prefix)
    provider_coll = db["providers"]
    quality_coll = db["provider_quality"]

    state_filter = {"business_address.state": {"$in": states}, "can_prescribe.drugs": {"$exists": True}}
    total = provider_coll.count_documents(state_filter)
    _log.info("Enriching %d providers with crosswalk data (states=%s)", total, states)

    # ── Phase 1: Build crosswalk for all unique molecules ─────────────

    _log.info("Phase 1: Collecting unique molecules...")
    molecule_set = set()
    cursor = provider_coll.find(state_filter, {"can_prescribe.drugs.molecule": 1, "_id": 0})
    for doc in cursor:
        drugs = doc.get("can_prescribe", {}).get("drugs", [])
        for drug in drugs:
            mol = drug.get("molecule", "").strip()
            if mol:
                molecule_set.add(mol)

    _log.info("Found %d unique molecules — building crosswalks...", len(molecule_set))

    crosswalk_map = {}  # molecule -> crosswalk result
    cache_hits = 0
    api_lookups = 0

    for mol in sorted(molecule_set):
        cached = _get_cached_crosswalk(env_prefix, mol)
        if cached and cached.get("crosswalk"):
            crosswalk_map[mol] = cached["crosswalk"]
            cache_hits += 1
        else:
            cw = build_crosswalk(mol, env_prefix)
            crosswalk_map[mol] = cw
            api_lookups += 1
            if api_lookups % 50 == 0:
                _log.info("  ...%d molecules looked up (%d cache hits)", api_lookups, cache_hits)

    _log.info("Phase 1 complete: %d crosswalks built (%d API, %d cache)",
              len(crosswalk_map), api_lookups, cache_hits)

    # ── Phase 2: Enrich each provider ─────────────────────────────────

    _log.info("Phase 2: Enriching providers...")
    cursor = provider_coll.find(
        state_filter,
        {"npi": 1, "can_prescribe": 1, "_id": 0},
    )

    provider_batch = []
    quality_batch = []
    enriched = 0

    for doc in cursor:
        npi = doc.get("npi", "")
        can_prescribe = doc.get("can_prescribe", {})
        drugs = can_prescribe.get("drugs", [])
        if not npi or not drugs:
            continue

        # Enrich each drug with crosswalk indications
        total_claims_all = 0
        total_on_label_claims = 0
        total_brand_claims = 0
        total_generic_claims = 0

        # Pathology tree: indication -> list of drugs treating it
        pathology_map = defaultdict(lambda: {
            "indication": "",
            "icd10_cm": [],
            "drugs": [],
            "total_claims": 0,
            "total_cost": 0.0,
        })

        for drug in drugs:
            mol = drug.get("molecule", "").strip()
            cw = crosswalk_map.get(mol, {})
            indications = cw.get("indications", [])

            # Write indications onto drug record
            drug["indications"] = [
                {
                    "indication": ind.get("indication", ""),
                    "icd10_cm": ind.get("icd10_cm", []),
                    "source": ind.get("source", "drugcentral"),
                }
                for ind in indications
            ]
            drug["icd10_codes"] = []
            for ind in indications:
                drug["icd10_codes"].extend(ind.get("icd10_cm", []))
            drug["icd10_codes"] = sorted(set(drug["icd10_codes"]))
            drug["rxcui"] = cw.get("rxcui")

            drug_claims = drug.get("total_claims", 0) or 0
            drug_brand = drug.get("brand_claims", 0) or 0
            drug_generic = drug.get("generic_claims", 0) or 0
            total_claims_all += drug_claims
            total_brand_claims += drug_brand
            total_generic_claims += drug_generic

            # A drug with indications is "on-label mappable"
            if indications:
                total_on_label_claims += drug_claims

            # Build pathology tree entries
            for ind in indications:
                key = ind.get("indication", "Unknown")
                entry = pathology_map[key]
                entry["indication"] = key
                entry["icd10_cm"] = sorted(set(entry["icd10_cm"] + ind.get("icd10_cm", [])))
                entry["drugs"].append({
                    "molecule": mol,
                    "brand_names": drug.get("brand_names", []),
                    "claims": drug_claims,
                })
                entry["total_claims"] += drug_claims

        # Build pathologies list with frequency bands
        pathologies = []
        for key, entry in pathology_map.items():
            freq_pct = (entry["total_claims"] / total_claims_all * 100) if total_claims_all > 0 else 0
            entry["frequency_band"] = _compute_band(freq_pct)
            pathologies.append(entry)

        pathologies.sort(key=lambda p: p["total_claims"], reverse=True)

        # Compute cost bands per pathology (% of total cost this pathology represents)
        # For now, approximate cost from claims since we have claims but not per-pathology cost
        for path in pathologies:
            cost_pct = (path["total_claims"] / total_claims_all * 100) if total_claims_all > 0 else 0
            path["cost_band"] = _compute_band(cost_pct)

        # Provider-level bands
        on_label_pct = (total_on_label_claims / total_claims_all * 100) if total_claims_all > 0 else 0
        on_label_band = _compute_band(on_label_pct)

        generic_total = total_brand_claims + total_generic_claims
        generic_pct = (total_generic_claims / generic_total * 100) if generic_total > 0 else 0
        generic_ratio_band = _compute_band(generic_pct)

        # Write 1: Update providers.can_prescribe
        provider_update = {
            "can_prescribe.drugs": drugs,
            "can_prescribe.cost_measures.pathologies": pathologies,
            "can_prescribe.cost_measures.on_label_band": on_label_band,
            "can_prescribe.cost_measures.generic_ratio_band": generic_ratio_band,
            "can_prescribe.crosswalk_version": CROSSWALK_VERSION,
            "can_prescribe.crosswalk_enriched_at": _now_iso(),
        }
        provider_batch.append(UpdateOne({"npi": npi}, {"$set": provider_update}))

        # Write 2: Dual-write to provider_quality
        quality_update = {
            "measures.prescriber_behavior.drugs": drugs,
            "measures.prescriber_behavior.cost_measures.pathologies": pathologies,
            "measures.prescriber_behavior.cost_measures.on_label_band": on_label_band,
            "measures.prescriber_behavior.cost_measures.generic_ratio_band": generic_ratio_band,
            "crosswalk_version": CROSSWALK_VERSION,
            "crosswalk_enriched_at": _now_iso(),
        }
        quality_batch.append(UpdateOne({"npi": npi}, {"$set": quality_update}))

        enriched += 1

        if len(provider_batch) >= batch_size:
            provider_coll.bulk_write(provider_batch, ordered=False)
            quality_coll.bulk_write(quality_batch, ordered=False)
            _log.info("Enriched %d / %d providers", enriched, total)
            provider_batch = []
            quality_batch = []

    # Flush remaining
    if provider_batch:
        provider_coll.bulk_write(provider_batch, ordered=False)
    if quality_batch:
        quality_coll.bulk_write(quality_batch, ordered=False)

    _log.info("Crosswalk enrichment complete: %d providers, %d molecules (%d API, %d cache)",
              enriched, len(crosswalk_map), api_lookups, cache_hits)

    return {
        "status": "complete",
        "providers_enriched": enriched,
        "unique_molecules": len(crosswalk_map),
        "api_lookups": api_lookups,
        "cache_hits": cache_hits,
        "crosswalk_version": CROSSWALK_VERSION,
    }


# ── Specialty Normalization ──────────────────────────────────────────────
# EPIC-006-F-010-S-004-REQ-T-014, REQ-018: peer-normalized cost measures.


def compute_specialty_baselines(
    env_prefix: str = "dev",
    states: list[str] | None = None,
):
    """Compute specialty-level baselines and normalize provider cost measures.

    Two levels of normalization:
      Level 1 — Molecule-level: For each molecule, compute the national average
        generic ratio across all providers who prescribe it. Store as
        molecule_generic_avg_band per drug on each provider.

      Level 2 — Specialty-level: For each taxonomy prefix (first 3 chars),
        compute the average generic ratio across all providers in that specialty.
        Store as specialty_generic_percentile on each provider — where this
        provider falls relative to their specialty peers (5% percentile bands).

    Runs AFTER enrich_providers_with_crosswalk — needs all providers enriched.
    """
    states = states or ["DE"]
    db = get_db(env_prefix)
    provider_coll = db["providers"]

    state_filter = {
        "business_address.state": {"$in": states},
        "can_prescribe.flagged": True,
        "can_prescribe.drugs": {"$exists": True},
    }

    _log.info("Computing specialty baselines (states=%s)...", states)

    # ── Level 1: Molecule-level national generic ratios ──────────────
    _log.info("Level 1: Computing molecule-level generic averages...")

    molecule_stats = defaultdict(lambda: {"brand": 0, "generic": 0})

    cursor = provider_coll.find(state_filter, {
        "can_prescribe.drugs": 1, "_id": 0,
    })
    for doc in cursor:
        for drug in doc.get("can_prescribe", {}).get("drugs", []):
            mol = drug.get("molecule", "")
            if mol:
                molecule_stats[mol]["brand"] += drug.get("brand_claims", 0) or 0
                molecule_stats[mol]["generic"] += drug.get("generic_claims", 0) or 0

    # Compute national average generic ratio per molecule
    molecule_avg = {}
    for mol, stats in molecule_stats.items():
        total = stats["brand"] + stats["generic"]
        if total > 0:
            pct = stats["generic"] / total * 100
            molecule_avg[mol] = _compute_band(pct)

    _log.info("  %d molecules with national averages", len(molecule_avg))

    # ── Level 2: Specialty-level provider generic ratios ─────────────
    _log.info("Level 2: Computing specialty baselines...")

    specialty_ratios = defaultdict(list)  # taxonomy_prefix -> list of generic %

    cursor = provider_coll.find(state_filter, {
        "npi": 1,
        "can_prescribe.cost_measures.generic_ratio_band": 1,
        "can_prescribe.drugs": 1,
        "taxonomies": 1,
        "_id": 0,
    })

    provider_data = []
    for doc in cursor:
        npi = doc.get("npi", "")
        taxonomies = doc.get("taxonomies", [])
        # primary_taxonomy_code lives top-level per LLD v39 sec. 7.1.
        # Fall back to first taxonomy code only if the top-level field
        # is absent (e.g., legacy record shape).
        tax_code = (doc.get("primary_taxonomy_code") or "").strip()
        if not tax_code and taxonomies:
            tax_code = (taxonomies[0].get("code") or "").strip()
        # Use first 3 chars as specialty group (e.g., "207" = physicians)
        tax_prefix = tax_code[:3] if len(tax_code) >= 3 else tax_code

        drugs = doc.get("can_prescribe", {}).get("drugs", [])
        total_brand = sum(d.get("brand_claims", 0) or 0 for d in drugs)
        total_generic = sum(d.get("generic_claims", 0) or 0 for d in drugs)
        total = total_brand + total_generic
        generic_pct = (total_generic / total * 100) if total > 0 else 0

        if tax_prefix:
            specialty_ratios[tax_prefix].append(generic_pct)

        provider_data.append({
            "npi": npi,
            "tax_prefix": tax_prefix,
            "generic_pct": generic_pct,
            "drugs": drugs,
        })

    _log.info("  %d specialty groups", len(specialty_ratios))

    # Compute percentile bands per specialty
    import bisect
    specialty_sorted = {}
    for prefix, ratios in specialty_ratios.items():
        specialty_sorted[prefix] = sorted(ratios)

    # ── Write back: molecule avg + specialty percentile ──────────────
    _log.info("Writing specialty-normalized measures to providers...")

    batch = []
    written = 0

    for pdata in provider_data:
        npi = pdata["npi"]
        tax_prefix = pdata["tax_prefix"]
        generic_pct = pdata["generic_pct"]

        # Specialty percentile
        specialty_percentile_band = None
        if tax_prefix in specialty_sorted:
            sorted_list = specialty_sorted[tax_prefix]
            rank = bisect.bisect_left(sorted_list, generic_pct)
            percentile = (rank / len(sorted_list) * 100) if sorted_list else 0
            specialty_percentile_band = _compute_band(percentile)

        # Molecule-level averages onto each drug
        drugs = pdata["drugs"]
        for drug in drugs:
            mol = drug.get("molecule", "")
            drug["molecule_generic_avg_band"] = molecule_avg.get(mol)

        update = {
            "can_prescribe.cost_measures.specialty_generic_percentile": specialty_percentile_band,
            "can_prescribe.cost_measures.specialty_group": tax_prefix,
            "can_prescribe.drugs": drugs,
        }
        batch.append(UpdateOne({"npi": npi}, {"$set": update}))
        written += 1

        if len(batch) >= 100:
            provider_coll.bulk_write(batch, ordered=False)
            _log.info("  Written %d / %d providers", written, len(provider_data))
            batch = []

    if batch:
        provider_coll.bulk_write(batch, ordered=False)

    _log.info("Specialty normalization complete: %d providers, %d specialty groups, %d molecules",
              written, len(specialty_sorted), len(molecule_avg))

    return {
        "status": "complete",
        "providers": written,
        "specialty_groups": len(specialty_sorted),
        "molecules_with_averages": len(molecule_avg),
    }


# ── CLI Entry Point ──────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Build molecule-to-indication-to-ICD10 crosswalk from public data sources."
    )
    parser.add_argument("--env", default="dev", help="Environment prefix (default: dev)")
    parser.add_argument("--states", default="DE", help="Comma-separated state codes (default: DE)")
    parser.add_argument("--molecule", help="Build crosswalk for a single molecule (diagnostic mode)")
    parser.add_argument("--batch-size", type=int, default=100, help="Batch size for bulk writes")
    args = parser.parse_args()


    if args.molecule:
        # Single-molecule diagnostic mode
        _log.info("Building crosswalk for: %s", args.molecule)
        result = build_crosswalk(args.molecule, args.env)
        import json
        ChatHealthyLoggingService().info(json.dumps(result, indent=2))
        return

    # Full batch enrichment
    states = [s.strip().upper() for s in args.states.split(",")]
    _log.info("Starting crosswalk enrichment: env=%s, states=%s", args.env, states)

    result = enrich_providers_with_crosswalk(
        env_prefix=args.env,
        states=states,
        batch_size=args.batch_size,
    )

    _log.info("Result: %s", result)


if __name__ == "__main__":
    main()
