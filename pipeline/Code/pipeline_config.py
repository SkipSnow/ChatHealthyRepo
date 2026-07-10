# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""PipelineConfig loader — reads chathealthyfrontend.pipeline.config."""

from __future__ import annotations

import os
from typing import Any

CONFIG_DB = "chathealthyfrontend"
CONFIG_COLL = "pipeline.config"

_DEFAULT_PROVIDER_CONFIG: dict[str, Any] = {
    "pipeline_name": "provider",
    "env": "dev",
    "source_freshness": [
        {"source_name": "nppes_npi", "number_of_days": 30},
        {"source_name": "census_zcta_county", "number_of_days": 365},
        {"source_name": "usda_rucc", "number_of_days": 365},
        {"source_name": "icd10_cm", "number_of_days": 90},
        {"source_name": "nucc", "number_of_days": 90},
    ],
    "failure_thresholds": {"discrepancy_abort": 1000},
    "batch_limits": {"normalize_batch_size": 1000},
}


def load_pipeline_config(mongo_client, env_prefix: str = "dev") -> dict[str, Any]:
    coll = mongo_client[CONFIG_DB][CONFIG_COLL]
    doc = coll.find_one({"env": env_prefix}) or coll.find_one({"env": "dev"})
    if doc:
        doc.pop("_id", None)
        return doc
    target = f"{env_prefix}_PublicHealthData.providers_v1"
    return {
        "env": env_prefix,
        "dataset_versions": {"provider_write_target": target},
        "source_freshness": [
            {"source_name": "nppes_npi", "number_of_days": 30},
            {"source_name": "census_zcta_county", "number_of_days": 365},
            {"source_name": "usda_rucc", "number_of_days": 365},
            {"source_name": "icd10_cm", "number_of_days": 90},
            {"source_name": "nucc", "number_of_days": 90},
        ],
        "failure_thresholds": {"discrepancy_abort": 1000},
        "batch_limits": {"normalize_batch_size": 1000},
        "throttle_rates": {},
        "notification_receivers": [],
    }


def ensure_pipeline_config(mongo_client, env_prefix: str = "dev") -> dict[str, Any]:
    coll = mongo_client[CONFIG_DB][CONFIG_COLL]
    target = f"{env_prefix}_PublicHealthData.providers_v1"
    defaults = load_pipeline_config(mongo_client, env_prefix)
    defaults.setdefault("dataset_versions", {})["provider_write_target"] = target
    coll.update_one(
        {"env": env_prefix},
        {"$setOnInsert": {"env": env_prefix}, "$set": {
            "dataset_versions.provider_write_target": target,
            "failure_thresholds": defaults.get("failure_thresholds"),
            "batch_limits": defaults.get("batch_limits"),
            "source_freshness": defaults.get("source_freshness"),
        }},
        upsert=True,
    )
    doc = coll.find_one({"env": env_prefix}) or defaults
    doc.pop("_id", None)
    return doc
