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
import logging
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
        _mongo = MongoClient(os.environ["MONGO_connectionString"])
    return _mongo


class DataFetcherBase:
    source_name: str = NotImplemented
    source_url: str = NotImplemented

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.container = self.config.get("blob_container", "provider-data")

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
        registry = self._load_registry()
        remote_sig = self._remote_signature()

        if registry and remote_sig and registry.get("remote_signature") == remote_sig:
            # verify blob actually exists before skipping
            blob_path = registry["blob_path"]
            try:
                service = get_blob_service()
                service.get_container_client(self.container).get_blob_client(blob_path).get_blob_properties()
                logging.info(
                    "[%s] Remote signature unchanged (%s), blob exists — skipping download.",
                    self.source_name, remote_sig,
                )
                return {
                    "blob_path": blob_path,
                    "version": registry.get("version", remote_sig),
                    "skipped": True,
                    "checksum_sha256": registry.get("checksum_sha256", ""),
                }
            except Exception:
                logging.warning(
                    "[%s] Registry says skip but blob '%s' not found — re-downloading.",
                    self.source_name, blob_path,
                )

        logging.info(
            "[%s] Downloading from %s (signature: %s → %s)",
            self.source_name, self.source_url,
            registry.get("remote_signature", "none") if registry else "none",
            remote_sig or "unknown",
        )

        blob_path, checksum, size_bytes = self._download_to_blob()
        version = remote_sig or checksum[:16]

        self._update_registry(
            blob_path=blob_path,
            checksum=checksum,
            remote_signature=remote_sig,
            version=version,
            size_bytes=size_bytes,
        )

        logging.info(
            "[%s] Downloaded %d bytes → blob: %s  sha256: %s…",
            self.source_name, size_bytes, blob_path, checksum[:16],
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
            logging.warning("[%s] HEAD request failed: %s", self.source_name, exc)
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
