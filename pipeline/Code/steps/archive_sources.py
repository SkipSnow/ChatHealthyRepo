# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""archive_sources — copy fetched blobs to permanent archive + update registry."""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException


from datetime import datetime, timezone

from blob_client import get_blob_service
from pipeline_db import get_mongo
from pipeline_source_archival import archive_source_blob, duplicate_version_detected

# Legacy fallback blob name for usda_rucc; retained as a literal after
# urban_flag.py was retired (RUCC now stamped as int by county_cascade).
_RUCC_JSON_BLOB_NAME = "rucc.json"

_log = ChatHealthyLoggingService()

# Map manifest source keys to registry source_name and default blob resolution.
_SOURCE_REGISTRY_KEYS = {
    "nppes_npi": "nppes_npi",
    "pl_pfile": "pl_pfile",
    "nucc": "nucc",
    "census_zcta_county": "census_zcta_county",
    "usda_rucc": "usda_rucc",
    "specialty_catalog": "specialty_catalog",
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
        return _RUCC_JSON_BLOB_NAME, "rucc.json"

    raise ChatHealthyException(mode="value_error", message=f"cannot resolve blob for source {source_key}")


def _source_container_or_raise(source_key: str, fetch_result: dict) -> str:
    """Return blob_container written by fetch_all_sources; raise if absent.

    Kept separate from execute() so Rule-005 statement 3 (log calls and
    raise ChatHealthyException in the same function body are forbidden)
    stays clean: the raise-bearing helper does not log, and execute()
    logs but does not raise ChatHealthyException.
    """
    source_container = fetch_result.get("blob_container")
    if source_container:
        return source_container
    raise ChatHealthyException(
        mode="fetch_result_missing_container",
        message=(
            f"fetch_result for source {source_key!r} lacks "
            "'blob_container'; archive cannot locate the uploaded blob. "
            "fetch_all_sources must always set blob_container."
        ),
        component="steps.archive_sources",
        source_key=source_key,
    )


def execute(ctx) -> dict:
    registry = get_mongo()["admin"]["DataSourceRegistry"]
    now = datetime.now(timezone.utc)
    blob_service = ctx.blob_client or get_blob_service()
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
        # The source blob's container is written by fetch_all_sources into
        # fetch_result['blob_container'] (transient container per env, e.g.
        # 'dev-pipeline-transients'). Prior archive code hard-coded
        # ctx.args.blob_container ('provider-data') which never matched
        # what the fetcher uploaded to, so archive raised FileNotFoundError
        # on the first source.
        source_container = _source_container_or_raise(source_key, fetch_result)
        try:
            blob_name, filename = _resolve_source_blob(source_key, fetch_result, reg_doc, source_container)
        except ValueError as exc:
            _log.warning("archive skip %s: %s", source_key, exc)
            continue

        try:
            result = archive_source_blob(
                blob_service,
                env_prefix=ctx.env_prefix,
                source_name=registry_name,
                version=version,
                source_container=source_container,
                source_blob_name=blob_name,
                filename=filename,
            )
        except FileNotFoundError as exc:
            _log.error("archive failed %s: %s", source_key, exc)
            raise

        source_version_identifier = fetch_result.get("source_version_identifier")
        registry.update_one(
            {"source_name": registry_name},
            {"$set": {
                "source_name": registry_name,
                "last_archived_at": now,
                "version": version,
                "archived_version": version,
                "source_version_identifier": source_version_identifier,
                "filename": result.get("archive_blob", "").rsplit("/", 1)[-1],
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
