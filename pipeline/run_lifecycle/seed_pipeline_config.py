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

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

_log = ChatHealthyLoggingService()

# finding_types: the severity map is authoritative (LLD v54 §7.5/§16). Each
# class carries fatal_at_count and max; both are left None here — the operator
# supplies the real numbers in a configuration update, and null means the
# class never escalates on volume / is unbounded.
PROVIDER_CONFIG = {
    "_id": "provider",
    "pipeline_name": "provider",
    "warning_threshold": 70000,
    "error_threshold": 30000,
    "failure_thresholds": {
        "discrepancy_abort": 100000
    },
    "provider_collection": "providers",
    "staging_collection": "provider_staging",
    "staging_complete": False,
    # metadata.subscribers_secret: the NAME of the Key Vault secret whose value
    # is the recipient list. The operator supplies the secret name; left null
    # here (a secret name is not invented).
    "metadata": {
        "subscribers_secret": None,
    },
    "discrepancy_report": {
        "report_cap_per_class": 25,
        "business_record_keys": {
            "provider": "npi",
            "specialty_metadata": "Code",
        },
        "finding_types": {
            "schema_violation": {"severity": "error", "fatal_at_count": None, "max": None},
            "missing_npi": {"severity": "error", "fatal_at_count": None, "max": None},
            "missing_entity_type_code": {"severity": "fatal", "fatal_at_count": None, "max": None},
            "invalid_npi": {"severity": "fatal", "fatal_at_count": None, "max": None},
            "invalid_entity_type_code": {"severity": "fatal", "fatal_at_count": None, "max": None},
            "error_specialty_embedding_failed": {"severity": "fatal", "fatal_at_count": None, "max": None},
            "unresolved_taxonomy_code": {"severity": "fatal", "fatal_at_count": None, "max": None},
            "county_unresolvable": {"severity": "warning", "fatal_at_count": None, "max": None},
            "authorized_official_incomplete": {"severity": "warning", "fatal_at_count": None, "max": None},
            "parent_organization_incomplete": {"severity": "warning", "fatal_at_count": None, "max": None},
            "state_missing_no_license": {"severity": "warning", "fatal_at_count": None, "max": None},
            "state_missing_ambiguous_license": {"severity": "warning", "fatal_at_count": None, "max": None},
            "coordinates_unresolvable": {"severity": "warning", "fatal_at_count": None, "max": None},
        },
    },
}

def seed_config(env_prefix: str) -> None:
    """Seed PipelineConfig in the one metadata database."""
    result = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")["pipelineAdmin"]["PipelineConfig"].replace_one(
        {"_id": "provider"},
        PROVIDER_CONFIG,
        upsert=True
    )
    _log.info(
        "seeded %s.PipelineConfig from %s: matched=%s modified=%s upserted_id=%s",
        "pipelineAdmin", env_prefix, result.matched_count,
        result.modified_count, result.upserted_id,
    )

if __name__ == "__main__":
    envs = sys.argv[1:] if len(sys.argv) > 1 else ["dev", "qa", "prod"]
    for env in envs:
        seed_config(env)
