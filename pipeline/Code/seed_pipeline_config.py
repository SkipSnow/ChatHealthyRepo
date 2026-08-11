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
sys.path.insert(0, os.path.join(repo_root, "ChatHealthyLib", "src"))
sys.path.insert(0, os.path.join(repo_root, "pipeline", "Code"))

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline_db import get_metadata_db, PIPELINE_ADMIN_DB

_log = ChatHealthyLoggingService()

PROVIDER_CONFIG = {
    "_id": "provider",
    "pipeline_name": "provider",
    "warning_threshold": 70000,
    "error_threshold": 30000,
    "failure_thresholds": {
        "discrepancy_abort": 100000
    },
    "warnings_errors_collection": "pipeline.discrepancy_reports",
    "provider_collection": "providers",
    "staging_collection": "provider_staging",
    "staging_complete": False,
}

def seed_config(env_prefix: str) -> None:
    """Seed PipelineConfig in the one metadata database."""
    result = get_metadata_db()["PipelineConfig"].replace_one(
        {"_id": "provider"},
        PROVIDER_CONFIG,
        upsert=True
    )
    _log.info(
        "seeded %s.PipelineConfig from %s: matched=%s modified=%s upserted_id=%s",
        PIPELINE_ADMIN_DB, env_prefix, result.matched_count,
        result.modified_count, result.upserted_id,
    )

if __name__ == "__main__":
    envs = sys.argv[1:] if len(sys.argv) > 1 else ["dev", "qa", "prod"]
    for env in envs:
        seed_config(env)
