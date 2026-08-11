from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
from chathealthy_lib.exceptions import ChatHealthyException
# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""DataFetcherBase — base class for all data source downloaders.

Checksum/ETag guard: before downloading, checks the remote source for an
ETag or Last-Modified header and compares it against the last known value
stored in admin.DataSourceRegistry. If unchanged, skips the download and
returns the existing blob path. If changed (or no record exists), downloads,
computes SHA-256, updates the registry, and uploads to blob storage.

Subclasses implement:
    source_name   : str  — unique identifier (e.g. "nppes_npi", "icd10")
    source_url    : str  — URL to download from
    blob_name()   : str  — blob filename to store in Azure
    parse()       : called after download to do any format-specific work

Usage:
    class NppesFetcher(DataFetcherBase):
        source_name = "nppes_npi"
        source_url  = "https://download.cms.gov/nppes/..."
        def blob_name(self): return "npi_latest.zip"
"""

import hashlib

import os
import tempfile
from datetime import datetime, timezone

import requests
from blob_client import get_blob_service
from pymongo import MongoClient

REGISTRY_COLLECTION = "admin.DataSourceRegistry"

_mongo: MongoClient | None = None


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "frontEnd")
    return _mongo


class DataFetcherBase:
    source_name: str = NotImplemented
    source_url: str = NotImplemented
    # Each subclass MUST declare its own staleness TTL. Staleness is a
    # per-source attribute (different sources update at different cadences:
    # NPPES weekly, Census ZCTA decennial, etc.). There is no base default.
    # The runtime value can also be overridden by config (operator-set).
    MAX_CACHE_AGE_DAYS: int = NotImplemented

    def __init__(self, config: dict = None, registry=None):
        self.config = config or {}
        self.container = self.config.get("blob_container", "provider-data")
        # registry threads PipelineDatasetRegistry through to subclasses whose
        # source_url is discovery-driven; discovery-owning subclasses look up
        # their own source_name via self._registry.resolve_source_url(...).
        self._registry = registry

    def _max_cache_age_days(self) -> int:
        """Resolve the effective staleness TTL for this source.

        The orchestrator passes 'source_staleness' as a list of
        {source_name, number_of_days} entries. Each fetcher looks up its
        own source_name. Subclass MAX_CACHE_AGE_DAYS is a fallback if the
        array is not supplied (e.g., legacy callers); the array is the
        canonical configuration path."""
        staleness = self.config.get("source_staleness") or []
        for entry in staleness:
            if isinstance(entry, dict) and entry.get("source_name") == self.source_name:
                return int(entry.get("number_of_days"))
        v = self.MAX_CACHE_AGE_DAYS
        if v is NotImplemented or v is None:
            raise ChatHealthyException(mode="runtime_error", message=f"{self.source_name}: no staleness TTL configured "
                f"(supply via source_staleness array or set "
                f"MAX_CACHE_AGE_DAYS on the subclass)"
            )
        return int(v)

    # ── Subclass hook for lazy URL discovery ──────────────────────────────────

    def _resolve_source_url(self) -> str:
        """Return the source URL to fetch. Called only when cache is missing
        or stale. Subclasses with expensive/flaky URL discovery override this
        and leave class-level source_url empty so discovery never fires on
        the cache-hit path."""
        return self.source_url

    # ── Public entry point ────────────────────────────────────────────────────

    def fetch(self) -> dict:
        """Download source if changed. Returns result dict.

        Returns:
            {
                "blob_path": str,       — blob name in Azure storage
                "version": str,         — etag / last-modified / sha256 prefix
                "skipped": bool,        — True if file was unchanged
                "checksum_sha256": str, — SHA-256 of downloaded file
            }
        """
        # Per F-102-S-003-REQ-B-002: each source has its own TTL (in days)
        # resolved from the source_staleness array in config. 0 = always
        # re-fetch every invocation. >0 = use cached blob if its last_modified
        # is within N days. If the cache is stale AND remote re-fetch fails,
        # the stale blob is DELETED and the activity aborts (no fallback).
        max_age_days = self._max_cache_age_days()
        blob_name = self.blob_name()
        bc = (
            get_blob_service()
            .get_container_client(self.container)
            .get_blob_client(blob_name)
        )
        blob_exists = False
        try:
            blob_exists = bc.exists()
        except Exception as exc:
            pass

        if blob_exists and max_age_days > 0:
            try:
                props = bc.get_blob_properties()
                age_days = (datetime.now(timezone.utc) - props.last_modified).days
                if age_days < max_age_days:
                    return {
                        "blob_path": blob_name,
                        "version": "cached",
                        "skipped": True,
                        "checksum_sha256": "",
                    }
            except Exception as exc:
                pass

        # Cache is stale OR missing OR TTL=0. Try remote re-fetch. Per REQ-B-002,
        # if re-fetch fails, delete the stale blob and abend.
        try:
            if not self.source_url or self.source_url is NotImplemented:
                self.source_url = self._resolve_source_url()
        except Exception as exc:
            if blob_exists:
                try:
                    bc.delete_blob()
                except Exception as del_exc:
                    pass
            raise ChatHealthyException(mode="runtime_error", message=f"[{self.source_name}] gather failed and stale blob deleted; pipeline must abend.") from exc

        try:
            registry = self._load_registry()
            remote_sig = self._remote_signature()

            if registry and remote_sig and registry.get("remote_signature") == remote_sig:
                # verify blob actually exists before skipping
                blob_path = registry["blob_path"]
                try:
                    service = get_blob_service()
                    service.get_container_client(self.container).get_blob_client(blob_path).get_blob_properties()
                    return {
                        "blob_path": blob_path,
                        "version": registry.get("version", remote_sig),
                        "skipped": True,
                        "checksum_sha256": registry.get("checksum_sha256", ""),
                    }
                except Exception:
                    pass


            blob_path, checksum, size_bytes = self._download_to_blob()
            version = remote_sig or checksum[:16]
        except Exception as exc:
            if blob_exists:
                try:
                    bc.delete_blob()
                except Exception as del_exc:
                    pass
            raise

        self._update_registry(
            blob_path=blob_path,
            checksum=checksum,
            remote_signature=remote_sig,
            version=version,
            size_bytes=size_bytes,
        )


        return {
            "blob_path": blob_path,
            "version": version,
            "skipped": False,
            "checksum_sha256": checksum,
        }

    # ── Subclass API ──────────────────────────────────────────────────────────

    def blob_name(self) -> str:
        """Return the blob filename to store in Azure. Override in subclass."""
        raise NotImplementedError

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _remote_signature(self) -> str | None:
        """HTTP HEAD to get ETag or Last-Modified. Returns None if unavailable."""
        try:
            resp = requests.head(self.source_url, timeout=15, allow_redirects=True)
            resp.raise_for_status()
            sig = resp.headers.get("ETag") or resp.headers.get("Last-Modified")
            return sig.strip('"') if sig else None
        except Exception as exc:
            ChatHealthyLoggingService().warning("[%s] HEAD request failed: %s", self.source_name, exc)
            return None

    def _download_to_blob(self) -> tuple[str, str, int]:
        """Stream download → compute SHA-256 → upload to blob.
        Returns (blob_name, sha256_hex, size_bytes).
        """
        name = self.blob_name()
        service = get_blob_service()
        try:
            service.get_container_client(self.container).create_container()
        except Exception:
            pass  # already exists

        blob_client = service.get_container_client(self.container).get_blob_client(name)
        sha256 = hashlib.sha256()
        size_bytes = 0

        with tempfile.TemporaryFile() as tmp:
            with requests.get(self.source_url, stream=True, timeout=600) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):  # 8MB chunks
                    tmp.write(chunk)
                    sha256.update(chunk)
                    size_bytes += len(chunk)

            tmp.seek(0)
            blob_client.upload_blob(tmp, overwrite=True)

        return name, sha256.hexdigest(), size_bytes

    def _load_registry(self) -> dict | None:
        """Load existing registry entry for this source."""
        db_name, coll_name = REGISTRY_COLLECTION.split(".", 1)
        return _get_mongo_client()[db_name][coll_name].find_one(
            {"source_name": self.source_name},
            {"_id": 0},
        )

    def _update_registry(
        self,
        blob_path: str,
        checksum: str,
        remote_signature: str | None,
        version: str,
        size_bytes: int,
    ) -> None:
        """Upsert registry entry for this source."""
        db_name, coll_name = REGISTRY_COLLECTION.split(".", 1)
        _get_mongo_client()[db_name][coll_name].update_one(
            {"source_name": self.source_name},
            {"$set": {
                "source_name": self.source_name,
                "source_url": self.source_url,
                "blob_path": blob_path,
                "checksum_sha256": checksum,
                "remote_signature": remote_signature,
                "version": version,
                "size_bytes": size_bytes,
                "last_downloaded": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )
