# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Prescriber Pipeline Manager — orchestrates the full prescriber behavior pipeline.
#
# Steps:
#   1. Fetch — Download CMS Part D + OIG LEIE + SAM.gov
#   2. Load — Parse CMS CSV, filter by state, build provider_quality collection
#   3. Enrich — Drug indications (LLM), exclusion flags, location, taxonomy
#   4. Embed — Vector embeddings for drug/molecule search
#
# Same pattern as provider_load_manager.py.
# Delaware only for alpha test.

import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

_log = logging.getLogger("prescriber_pipeline")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)


def run_pipeline(config: dict = None):
    """Run the full prescriber behavior pipeline."""
    config = config or {}
    env_prefix = config.get("env_prefix", "dev")
    states = config.get("states", ["DE"])
    steps = config.get("steps", [1, 2, 3, 4, 5, 6])

    _log.info("=" * 60)
    _log.info("Prescriber Pipeline — %s", datetime.now(timezone.utc).isoformat())
    _log.info("Environment: %s", env_prefix)
    _log.info("States: %s", states)
    _log.info("Steps: %s", steps)
    _log.info("=" * 60)

    results = {}

    # ── Step 0: Validate ───────────────────────────────────────────────────
    if 0 in steps:
        _log.info("")
        _log.info("─── Step 0: Validate ───")
        from pipeline_db import get_db
        db = get_db(env_prefix)
        state_filter = {"practice_address.state": {"$in": states}}
        results["validate"] = {
            "status": "valid",
            "pipeline": "PrescriberEvaluateCarePipeline",
            "states": states,
            "collections": {
                "providers": db["providers"].count_documents(state_filter),
                "provider_quality": db["provider_quality"].count_documents({}),
                "drug_indication_cache": db["drug_indication_cache"].count_documents({}),
            },
        }
        _log.info("Validate result: %s", results["validate"])
        if steps == [0]:
            return results

    # ── Step 1: Fetch ──────────────────────────────────────────────────────
    if 1 in steps:
        _log.info("")
        _log.info("─── Step 1: Fetch source data ───")
        from prescriber_data_fetcher import fetch_all
        results["fetch"] = fetch_all(config)
        _log.info("Fetch result: %s", results["fetch"])

    # ── Step 2: Load ───────────────────────────────────────────────────────
    if 2 in steps:
        _log.info("")
        _log.info("─── Step 2: Load CMS Part D → provider_quality ───")
        from prescriber_load_worker import PrescriberLoadWorker

        worker_config = {
            "env_prefix": env_prefix,
            "states": states,
            "blob_name": "cms_partd_prescriber_latest.csv",
            "batch_size": 500,
        }
        worker = PrescriberLoadWorker(worker_config)
        results["load"] = worker.pipeline_execute()
        _log.info("Load result: %s", results["load"])

    # ── Step 3: Crosswalk enrichment ─────────────────────────────────────
    #   Replaces LLM-based drug→indication mapping with deterministic
    #   crosswalk from RxNorm + DrugCentral (+ UMLS when licensed).
    #   Also builds indication-first cost_measures tree with 5% bands.
    #   Dual-writes to providers and provider_quality.
    if 3 in steps:
        _log.info("")
        _log.info("─── Step 3: Crosswalk enrichment — molecule→indication→ICD-10 + cost bands ───")
        from crosswalk_builder import enrich_providers_with_crosswalk
        results["crosswalk"] = enrich_providers_with_crosswalk(
            env_prefix=env_prefix,
            states=states,
        )
        _log.info("Crosswalk result: %s", results["crosswalk"])

    # ── Step 4: OIG/SAM exclusion flags ──────────────────────────────────
    if 4 in steps:
        _log.info("")
        _log.info("─── Step 4: Exclusion flags — OIG LEIE + SAM.gov ───")
        from prescriber_enrichment_job import enrich_all
        results["exclusions"] = enrich_all(
            env_prefix=env_prefix,
            states=states,
            batch_size=100,
        )
        _log.info("Exclusion result: %s", results["exclusions"])

    # ── Step 5: Specialty normalization ───────────────────────────────────
    if 5 in steps:
        _log.info("")
        _log.info("─── Step 5: Specialty normalization — peer benchmarks ───")
        from crosswalk_builder import compute_specialty_baselines
        results["specialty"] = compute_specialty_baselines(
            env_prefix=env_prefix,
            states=states,
        )
        _log.info("Specialty result: %s", results["specialty"])

    # ── Step 6: Embed ──────────────────────────────────────────────────────
    if 6 in steps:
        _log.info("")
        _log.info("─── Step 6: Embed — drug/molecule vector search ───")
        results["embed"] = _embed_prescriber_data(env_prefix, states)
        _log.info("Embed result: %s", results["embed"])

    # ── Summary ────────────────────────────────────────────────────────────
    _log.info("")
    _log.info("=" * 60)
    _log.info("Pipeline complete")
    for step, result in results.items():
        status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
        _log.info("  %s: %s", step, status)
    _log.info("=" * 60)

    return results


def _compute_band(pct: float) -> str:
    """Convert a percentage (0-100) to a 5% band string. 20 bands total."""
    lower = int(pct // 5) * 5
    lower = min(lower, 95)
    return f"{lower}-{lower + 5}%"


def _compute_on_label_bands(env_prefix: str, states: list):
    """Step 4: Build pathology-first cost_measures tree + on-label band.

    After drug→indication enrichment (Step 3), this step:
    1. Reorganizes each provider's drugs into a pathology-first tree
       under can_prescribe.cost_measures.pathologies
    2. Computes national molecule frequency bands per pathology
    3. Computes provider-level on_label_band (5% bands, 20 total)

    Tree structure per provider:
      cost_measures.pathologies: [
        {
          indication: "Hyperlipidemia",
          icd10_codes: ["E78.0"],
          molecules: [
            {molecule, brand_claims, generic_claims, total_claims,
             national_frequency_band}
          ]
        }
      ]
      cost_measures.generic_ratio_band: "85-90%"   (set in Step 2)
      cost_measures.on_label_band: "80-85%"         (set here)

    EPIC-006-F-010-S-004-REQ-T-012.
    """
    from collections import defaultdict
    from pymongo import UpdateOne
    from pipeline_db import get_db

    db = get_db(env_prefix)
    provider_coll = db["providers"]
    quality_coll = db["provider_quality"]

    state_filter = {"practice_address.state": {"$in": states}}

    providers = list(provider_coll.find(
        {**state_filter, "can_prescribe.flagged": True, "can_prescribe.drugs": {"$exists": True}},
        {"npi": 1, "can_prescribe.drugs": 1, "can_prescribe.cost_measures": 1, "_id": 0},
    ))
    _log.info("Cost measures: %d prescribers with drug data", len(providers))

    if not providers:
        return {"status": "skip", "reason": "no prescribers with drug data"}

    # ── Pass 1: Compute national totals per molecule per indication ────────
    # Aggregate across all providers to get national frequency denominators
    national_indication_totals = defaultdict(int)  # indication → total claims nationally
    for prov in providers:
        for drug in prov.get("can_prescribe", {}).get("drugs", []):
            for ind in drug.get("indications", []):
                key = ind.get("indication", "Unknown")
                national_indication_totals[key] += drug.get("total_claims", 0)

    _log.info("  National indication totals: %d distinct indications", len(national_indication_totals))

    # ── Pass 2: Build pathology tree + bands per provider ─────────────────
    provider_batch = []
    quality_batch = []
    computed = 0

    for prov in providers:
        npi = prov["npi"]
        drugs = prov.get("can_prescribe", {}).get("drugs", [])

        # Build pathology → molecules map
        pathology_map = defaultdict(lambda: {"molecules": {}, "icd10_codes": set()})
        on_label_claims = 0
        off_label_claims = 0

        for drug in drugs:
            mol = drug.get("molecule", "")
            claims = drug.get("total_claims", 0)
            brand_claims = drug.get("brand_claims", 0)
            generic_claims = drug.get("generic_claims", 0)
            indications = drug.get("indications", [])

            if indications:
                on_label_claims += claims
                for ind in indications:
                    ind_name = ind.get("indication", "Unknown")
                    for code in ind.get("icd10_codes", []):
                        pathology_map[ind_name]["icd10_codes"].add(code)

                    if mol not in pathology_map[ind_name]["molecules"]:
                        pathology_map[ind_name]["molecules"][mol] = {
                            "molecule": mol,
                            "brand_claims": 0,
                            "generic_claims": 0,
                            "total_claims": 0,
                        }
                    entry = pathology_map[ind_name]["molecules"][mol]
                    entry["brand_claims"] += brand_claims
                    entry["generic_claims"] += generic_claims
                    entry["total_claims"] += claims
            else:
                off_label_claims += claims

        # Build pathologies array with national frequency bands
        pathologies = []
        for ind_name, data in pathology_map.items():
            nat_total = national_indication_totals.get(ind_name, 1)
            mols = []
            for mol_data in data["molecules"].values():
                mol_pct = (mol_data["total_claims"] / nat_total * 100) if nat_total > 0 else 0
                mol_data["national_frequency_band"] = _compute_band(mol_pct)
                mols.append(mol_data)
            mols.sort(key=lambda m: m["total_claims"], reverse=True)

            pathologies.append({
                "indication": ind_name,
                "icd10_codes": sorted(data["icd10_codes"]),
                "molecules": mols,
            })
        pathologies.sort(key=lambda p: sum(m["total_claims"] for m in p["molecules"]), reverse=True)

        # On-label band
        total = on_label_claims + off_label_claims
        on_label_pct = (on_label_claims / total * 100) if total > 0 else 0
        on_label_band = _compute_band(on_label_pct)

        # Write to providers
        provider_batch.append(UpdateOne(
            {"npi": npi},
            {"$set": {
                "can_prescribe.cost_measures.pathologies": pathologies,
                "can_prescribe.cost_measures.on_label_band": on_label_band,
            }},
        ))
        quality_batch.append(UpdateOne(
            {"npi": npi},
            {"$set": {
                "measures.prescriber_behavior.cost_measures.pathologies": pathologies,
                "measures.prescriber_behavior.cost_measures.on_label_band": on_label_band,
            }},
        ))
        computed += 1

        if len(provider_batch) >= 500:
            provider_coll.bulk_write(provider_batch, ordered=False)
            if quality_batch:
                quality_coll.bulk_write(quality_batch, ordered=False)
            _log.info("  Cost measures: %d providers computed", computed)
            provider_batch = []
            quality_batch = []

    if provider_batch:
        provider_coll.bulk_write(provider_batch, ordered=False)
    if quality_batch:
        quality_coll.bulk_write(quality_batch, ordered=False)

    _log.info("Cost measures complete: %d providers, %d national indications",
              computed, len(national_indication_totals))
    return {
        "status": "complete",
        "providers_computed": computed,
        "national_indications": len(national_indication_totals),
    }


def _embed_prescriber_data(env_prefix: str, states: list):
    """Create text embeddings for drug/molecule search on provider_quality records."""
    from pymongo import UpdateOne
    from openai import OpenAI
    from pipeline_db import get_db

    db = get_db(env_prefix)
    quality_coll = db["provider_quality"]
    oai = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    cursor = quality_coll.find(
        {"measures.prescriber_behavior.drugs": {"$exists": True, "$ne": []}},
        {"npi": 1, "measures.prescriber_behavior.drugs": 1}
    )

    batch = []
    embedded = 0

    for doc in cursor:
        npi = doc["npi"]
        drugs = doc.get("measures", {}).get("prescriber_behavior", {}).get("drugs", [])

        # Build embedding text: molecules + brand names + indications
        parts = []
        for d in drugs:
            parts.append(d.get("molecule", ""))
            parts.extend(d.get("brand_names", []))
            parts.extend(d.get("generic_names", []))
            for ind in d.get("indications", []):
                parts.append(ind.get("indication", ""))

        text = " ".join(p for p in parts if p)
        if not text:
            continue

        # Truncate to 8000 chars for embedding model
        text = text[:8000]

        try:
            resp = oai.embeddings.create(
                model="text-embedding-3-small",
                input=text,
            )
            vector = resp.data[0].embedding

            batch.append(UpdateOne(
                {"npi": npi},
                {"$set": {
                    "prescriber_embedding": vector,
                    "prescriber_embedding_text": text[:500],  # store truncated text for debug
                }}
            ))
            embedded += 1

            if len(batch) >= 50:
                quality_coll.bulk_write(batch, ordered=False)
                _log.info("Embedded %d NPIs", embedded)
                batch = []

        except Exception as e:
            _log.warning("Embedding failed for NPI %s: %s", npi, e)

    if batch:
        quality_coll.bulk_write(batch, ordered=False)

    _log.info("Embedding complete: %d NPIs embedded", embedded)
    return {"status": "complete", "npis_embedded": embedded}


# ── CLI entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Prescriber Behavior Pipeline")
    parser.add_argument("--env", default="dev", help="Environment prefix (dev/qa/prod)")
    parser.add_argument("--states", default="DE", help="Comma-separated state codes")
    parser.add_argument("--steps", default="1,2,3,4,5,6", help="Comma-separated step numbers (1=fetch, 2=load, 3=crosswalk, 4=OIG/SAM, 5=specialty normalization, 6=embed)")
    args = parser.parse_args()

    config = {
        "env_prefix": args.env,
        "states": [s.strip() for s in args.states.split(",")],
        "steps": [int(s.strip()) for s in args.steps.split(",")],
    }

    result = run_pipeline(config)
    print("\nPipeline result:", result)
