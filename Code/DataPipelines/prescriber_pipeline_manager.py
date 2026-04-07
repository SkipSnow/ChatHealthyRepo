# Copyright (c) 2026 Skip Snow. All rights reserved.
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
    start_step = config.get("start_step", 1)

    _log.info("=" * 60)
    _log.info("Prescriber Pipeline — %s", datetime.now(timezone.utc).isoformat())
    _log.info("Environment: %s", env_prefix)
    _log.info("States: %s", states)
    _log.info("Start step: %d", start_step)
    _log.info("=" * 60)

    results = {}

    # ── Step 1: Fetch ──────────────────────────────────────────────────────
    if start_step <= 1:
        _log.info("")
        _log.info("─── Step 1: Fetch source data ───")
        from prescriber_data_fetcher import fetch_all
        results["fetch"] = fetch_all(config)
        _log.info("Fetch result: %s", results["fetch"])

    # ── Step 2: Load ───────────────────────────────────────────────────────
    if start_step <= 2:
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

    # ── Step 3: Enrich ─────────────────────────────────────────────────────
    if start_step <= 3:
        _log.info("")
        _log.info("─── Step 3: Enrich — indications, exclusions, location ───")
        from prescriber_enrichment_job import enrich_all
        results["enrich"] = enrich_all(
            env_prefix=env_prefix,
            states=states,
            batch_size=100,
        )
        _log.info("Enrich result: %s", results["enrich"])

    # ── Step 4: Embed ──────────────────────────────────────────────────────
    if start_step <= 4:
        _log.info("")
        _log.info("─── Step 4: Embed — drug/molecule vector search ───")
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
    parser.add_argument("--start-step", type=int, default=1, help="Start at step (1-4)")
    args = parser.parse_args()

    config = {
        "env_prefix": args.env,
        "states": [s.strip() for s in args.states.split(",")],
        "start_step": args.start_step,
    }

    result = run_pipeline(config)
    print("\nPipeline result:", result)
