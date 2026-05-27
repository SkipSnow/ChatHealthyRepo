# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""gather_activities - the six source-gather activity bodies.

Thin shims around existing source loaders. Each runs once per pipeline run before
record-processing starts.
"""
from __future__ import annotations

import logging
import os


def gather_nppes_zip_activity_fn(config: dict) -> dict:
    from provider_load_manager import download_zip_fn
    return {"source": "nppes_zip", "result": download_zip_fn(config)}


def gather_pl_pfile_activity_fn(config: dict) -> dict:
    from provider_load_manager import attach_practice_locations_fn
    try:
        result = attach_practice_locations_fn(config)
        return {"source": "pl_pfile", "result": result}
    except Exception as exc:
        logging.warning("gather_pl_pfile: %s", exc)
        return {"source": "pl_pfile", "error": str(exc)[:300]}


def gather_zip_crosswalk_activity_fn(config: dict) -> dict:
    from zip_county_crosswalk_loader import load_crosswalk
    return {"source": "zip_crosswalk", "result": load_crosswalk(config)}


def gather_rucc_activity_fn(config: dict) -> dict:
    from urban_flag import stamp_urban_flag_fn
    states = config.get("states") or []
    blob_container = config.get("blob_container", "provider-data")
    provider_collection = config.get(
        "provider_collection",
        f"{os.environ.get('ENV_PREFIX', 'dev')}_PublicHealthData.providers",
    )
    bulk_batch_size = int(config.get("bulk_batch_size", 1000))
    result = stamp_urban_flag_fn({
        "blob_container": blob_container,
        "states": states,
        "provider_collection": provider_collection,
        "bulk_batch_size": bulk_batch_size,
    })
    return {"source": "rucc", "result": result}


def gather_specialty_catalog_activity_fn(config: dict) -> dict:
    from load_specialty_data import run_load_specialty_data
    return {"source": "specialty_catalog", "result": run_load_specialty_data(config)}


def gather_icd10_activity_fn(config: dict) -> dict:
    from icd10_loader import load_icd10
    return {"source": "icd10", "result": load_icd10(config)}
