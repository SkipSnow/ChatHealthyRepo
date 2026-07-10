# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""archive_sources — copy fetched blobs to permanent archive + update registry."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from blob_client import get_blob_service
from pipeline_db import get_mongo
from pipeline_source_archival import archive_source_blob, duplicate_version_detected
from urban_flag import RUCC_JSON_BLOB_NAME

_log = logging.getLogger(__name__)

# Map manifest source keys to registry source_name and default blob resolution.
_SOURCE_REGISTRY_KEYS = {
    "nppes_npi": "nppes_npi",
    "pl_pfile": "pl_pfile",
    "census_zcta_county": "census_zcta_county",
    "usda_rucc": "usda_rucc",
    "specialty_catalog": "nucc",
    "icd10_cm": "icd10_cm",
}


def _resolve_source_blob(source_key: str, fetch_result: dict, registry_doc: dict, container: str) -> tuple[str, str]:
    """Return (blob_name, filename) for archival."""
    inner = fetch_result.get("result") or fetch_result
    if isinstance(inner, dict):
        if inner.get("zip_path"):
            return inner["zip_path"], inner["zip_path"].split("/")[-1]
        if inner.get("blob_path"):
            return inner["blob_path"], inner["blob_path"].split("/")[-1]
        if inner.get("result") and isinstance(inner["result"], dict):
            nested = inner["result"]
            if nested.get("zip_path"):
                return nested["zip_path"], nested["zip_path"].split("/")[-1]
            if nested.get("blob_path"):
                return nested["blob_path"], nested["blob_path"].split("/")[-1]

    if registry_doc.get("blob_path"):
        bp = registry_doc["blob_path"]
        return bp, bp.split("/")[-1]

    if source_key == "usda_rucc":
        return RUCC_JSON_BLOB_NAME, "rucc.json"

    raise ValueError(f"cannot resolve blob for source {source_key}")


def execute(ctx) -> dict:
    registry = get_mongo()["admin"]["DataSourceRegistry"]
    now = datetime.now(timezone.utc)
    blob_service = ctx.blob_client or get_blob_service()
    container = ctx.args.blob_container
    fetch_results = ctx.manifest.metrics.get("fetch_results") or {}
    versions = ctx.manifest.source_versions or {}
    archived: list[dict] = []

    for source_key, version in versions.items():
        registry_name = _SOURCE_REGISTRY_KEYS.get(source_key, source_key)
        reg_doc = registry.find_one({"source_name": registry_name}) or {}
        if duplicate_version_detected(reg_doc.get("archived_version"), version) and reg_doc.get("archive_blob"):
            _log.info("archive skip %s version %s already archived", source_key, version)
            archived.append({"source": source_key, "skipped": True, "version": version})
            continue

        fetch_result = fetch_results.get(source_key, {})
        try:
            blob_name, filename = _resolve_source_blob(source_key, fetch_result, reg_doc, container)
        except ValueError as exc:
            _log.warning("archive skip %s: %s", source_key, exc)
            continue

        try:
            result = archive_source_blob(
                blob_service,
                env_prefix=ctx.env_prefix,
                source_name=registry_name,
                version=version,
                source_container=container,
                source_blob_name=blob_name,
                filename=filename,
            )
        except FileNotFoundError as exc:
            _log.error("archive failed %s: %s", source_key, exc)
            raise

        registry.update_one(
            {"source_name": registry_name},
            {"$set": {
                "source_name": registry_name,
                "last_archived_at": now,
                "version": version,
                "archived_version": version,
                "archive_blob": result["archive_blob"],
                "archive_container": result["archive_container"],
                "archive_path": result["archive_path"],
                "checksum_sha256": result["checksum_sha256"],
                "run_id": ctx.run_id,
            }},
            upsert=True,
        )
        archived.append(result)

    ctx.manifest.metrics["archived_sources"] = archived
    return {"archived": archived, "count": len(archived)}
