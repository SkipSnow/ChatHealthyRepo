# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Pure helpers for source fetch archival naming — LLD §4.4 / §4.5."""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService

import hashlib

from typing import Any

_log = ChatHealthyLoggingService()

ARCHIVE_CONTAINER_SUFFIX = "-pipeline-sources"
MAX_RETAINED_VERSIONS = 2


def checksum_sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_segment(value: str) -> str:
    cleaned = value.replace("/", "_").replace("\\", "_")
    out: list[str] = []
    for ch in cleaned:
        keep = ch.isalnum() or ch == "_" or ch in ".-"
        if keep:
            out.append(ch)
        elif not out or out[-1] != "_":
            out.append("_")
    return "".join(out)


def archive_source_blob_path(
    *,
    env_prefix: str,
    source_name: str,
    version: str,
    filename: str,
) -> str:
    """Return {env}-pipeline-sources/{source_name}/{version}/{filename}."""
    safe_version = _safe_segment(version)
    safe_name = _safe_segment(source_name)
    safe_file = _safe_segment(filename)
    return f"{env_prefix}{ARCHIVE_CONTAINER_SUFFIX}/{safe_name}/{safe_version}/{safe_file}"


def sha256_matches_registry(local_sha256: str, registry_sha256: str | None) -> bool:
    if not registry_sha256:
        return False
    return local_sha256.lower() == registry_sha256.lower()


def duplicate_version_detected(existing_version: str | None, new_version: str) -> bool:
    return bool(existing_version) and existing_version == new_version


def archive_source_blob(
    blob_service,
    *,
    env_prefix: str,
    source_name: str,
    version: str,
    source_container: str,
    source_blob_name: str,
    filename: str | None = None,
) -> dict[str, Any]:
    """Copy a fetched source blob into the permanent archive layout (§4.5)."""
    src_client = blob_service.get_container_client(source_container)
    src_blob = src_client.get_blob_client(source_blob_name)
    if not src_blob.exists():
        raise FileNotFoundError(
            f"source blob missing: {source_container}/{source_blob_name}"
        )

    data = src_blob.download_blob().readall()
    checksum = checksum_sha256_bytes(data)
    archive_name = filename or source_blob_name.split("/")[-1]
    archive_path = archive_source_blob_path(
        env_prefix=env_prefix,
        source_name=source_name,
        version=version,
        filename=archive_name,
    )
    archive_container = f"{env_prefix}{ARCHIVE_CONTAINER_SUFFIX}"
    dest_client = blob_service.get_container_client(archive_container)
    try:
        dest_client.create_container()
    except Exception:
        pass
    dest_client.get_blob_client(archive_path.split("/", 1)[-1] if "/" in archive_path else archive_path)
    # archive_path is full logical path; blob name is everything after container prefix
    blob_name = archive_path.split(f"{env_prefix}{ARCHIVE_CONTAINER_SUFFIX}/", 1)[-1]
    dest_client.upload_blob(name=blob_name, data=data, overwrite=True)

    rotate_archived_versions(
        blob_service,
        env_prefix=env_prefix,
        source_name=source_name,
        keep_versions={version},
    )
    return {
        "source_name": source_name,
        "version": version,
        "archive_container": archive_container,
        "archive_blob": blob_name,
        "archive_path": archive_path,
        "checksum_sha256": checksum,
        "size_bytes": len(data),
    }


def rotate_archived_versions(
    blob_service,
    *,
    env_prefix: str,
    source_name: str,
    keep_versions: set[str],
) -> list[str]:
    """Retain current version + one prior per LLD §4.5 / F-001-S-010."""
    container = f"{env_prefix}{ARCHIVE_CONTAINER_SUFFIX}"
    prefix = f"{_safe_segment(source_name)}/"
    cc = blob_service.get_container_client(container)
    versions: dict[str, list[str]] = {}
    for blob in cc.list_blobs(name_starts_with=prefix):
        parts = blob.name.split("/")
        if len(parts) < 2:
            continue
        ver = parts[1]
        versions.setdefault(ver, []).append(blob.name)

    deleted: list[str] = []
    sorted_versions = sorted(versions.keys(), reverse=True)
    retain = set()
    for ver in sorted_versions:
        if ver in keep_versions or len(retain) < MAX_RETAINED_VERSIONS:
            retain.add(ver)

    for ver, names in versions.items():
        if ver in retain:
            continue
        for name in names:
            cc.delete_blob(name)
            deleted.append(name)
            _log.info("rotated archive blob %s/%s", container, name)
    return deleted
