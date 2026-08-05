#!/usr/bin/env python3
"""Seed PipelineConfig collection with provider pipeline configuration."""

import sys
import os
from dotenv import load_dotenv

# Load .env before any imports
repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
env_file = os.path.join(repo_root, ".env")
if os.path.exists(env_file):
    load_dotenv(env_file)

# Add paths for imports
sys.path.insert(0, os.path.join(repo_root, "FrontEndApplicationLib", "src"))
sys.path.insert(0, os.path.join(repo_root, "pipeline", "Code"))

from pipeline_db import get_db

PROVIDER_CONFIG = {
    "_id": "provider",
    "pipeline_name": "provider",
    "warning_threshold": 70000,
    "error_threshold": 30000,
    "failure_thresholds": {
        "discrepancy_abort": 100000
    }
}

def seed_config(env_prefix: str) -> None:
    """Seed PipelineConfig for given environment."""
    db = get_db(env_prefix)
    result = db["PipelineConfig"].replace_one(
        {"_id": "provider"},
        PROVIDER_CONFIG,
        upsert=True
    )
    print(f"{env_prefix}: matched={result.matched_count}, modified={result.modified_count}, upserted_id={result.upserted_id}")

if __name__ == "__main__":
    envs = sys.argv[1:] if len(sys.argv) > 1 else ["dev", "qa", "prod"]
    for env in envs:
        seed_config(env)
        print(f"✓ Seeded {env}")
