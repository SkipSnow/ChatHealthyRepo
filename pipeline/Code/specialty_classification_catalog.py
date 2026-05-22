"""Single-source-of-truth loader for the proprietary NUCC classification catalog.

Maps every NUCC taxonomy code to two booleans:
  - can_prescribe   (true if the role has prescribing authority)
  - is_homeopathic  (true if the role is homeopathic)

The catalog lives in Azure Blob Storage at
  findcarestorage / chathealthy-public-data / enrichment_content/specialty_classification_gpt41.json

It is the single source of truth for both pipelines: the Provider pipeline
denormalizes both flags onto each provider record (via the provider's primary
taxonomy code), and the Specialty pipeline writes the flags onto each
SpecialtyMetaData record.

No fallbacks. Blob read failure raises.

The JSON file on disk uses key `homeopathic`; the catalog returned by
load_catalog() maps it to `is_homeopathic` to match the field name used on
provider records.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Dict

from azure.storage.blob import BlobServiceClient

_BLOB_CONTAINER = "chathealthy-public-data"
_BLOB_NAME = "enrichment_content/specialty_classification_gpt41.json"

_cache: Dict[str, Dict[str, bool]] | None = None
_cache_lock = threading.Lock()


def load_catalog() -> Dict[str, Dict[str, bool]]:
    """Return {nucc_code: {"can_prescribe": bool, "is_homeopathic": bool}}.

    Cached for the lifetime of the process.
    """
    global _cache
    if _cache is not None:
        return _cache
    with _cache_lock:
        if _cache is not None:
            return _cache
        conn = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
        svc = BlobServiceClient.from_connection_string(conn)
        bc = svc.get_blob_client(container=_BLOB_CONTAINER, blob=_BLOB_NAME)
        raw = bc.download_blob().readall()
        doc = json.loads(raw)
        entries = doc.get("classifications") or []
        if not entries:
            raise RuntimeError(
                f"specialty_classification_catalog: blob "
                f"{_BLOB_CONTAINER}/{_BLOB_NAME} returned 0 classifications"
            )
        out: Dict[str, Dict[str, bool]] = {}
        for row in entries:
            code = row.get("Code")
            if not code:
                continue
            out[code] = {
                "can_prescribe": bool(row.get("can_prescribe", False)),
                "is_homeopathic": bool(row.get("homeopathic", False)),
            }
        _cache = out
        return _cache


def clear_cache() -> None:
    """Test-only: force the next load_catalog() to re-read the blob."""
    global _cache
    with _cache_lock:
        _cache = None
