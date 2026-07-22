"""Single-source-of-truth loader for the proprietary NUCC classification catalog.

Maps every NUCC taxonomy code to four booleans:
  - can_prescribe     (true if the role has prescribing authority)
  - is_homeopathic    (true if the role is homeopathic)
  - is_npi_registered (true if the code appears as a taxonomy on at least
                       one provider record in NPPES)
  - is_disqualified   (true if the code represents a non-clinical role —
                       curated per BUG-005 from the NUCC Grouping field;
                       codes under "Other Service Providers" or
                       "Student, Health Care" are disqualified)

The catalog lives in the MongoDB collection
`ProprietaryData.SpecialtyAndProviderFlags` on the pipeline cluster
(ChatHealthyDataPipelines), per EPIC-010-F-105-S-001-REQ-T-001. The
Provider pipeline denormalizes all three flags onto each provider
record (via the provider's primary taxonomy code), and the Specialty
pipeline writes the flags onto each SpecialtyMetaData record.

No fallbacks. Mongo read failure raises.
"""
from __future__ import annotations
from chathealthy_frontend_lib.exceptions import ChatHealthyException
from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities

import os
import threading
from typing import Dict

from pymongo import MongoClient

_DB_NAME = "ProprietaryData"
_COLL_NAME = "SpecialtyAndProviderFlags"

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
        conn = os.environ["MONGO_connectionString"]
        client = ChatHealthyMongoUtilities("MONGO_connectionString").getConnection()
        try:
            cursor = client[_DB_NAME][_COLL_NAME].find(
                {}, {"_id": 0, "Code": 1, "can_prescribe": 1,
                     "is_homeopathic": 1, "is_npi_registered": 1,
                     "is_disqualified": 1}
            )
            out: Dict[str, Dict[str, bool]] = {}
            for row in cursor:
                code = row.get("Code")
                if not code:
                    continue
                out[code] = {
                    "can_prescribe": bool(row.get("can_prescribe", False)),
                    "is_homeopathic": bool(row.get("is_homeopathic", False)),
                    "is_npi_registered": bool(row.get("is_npi_registered", False)),
                    "is_disqualified": bool(row.get("is_disqualified", False)),
                }
        finally:
            client.close()
        if not out:
            raise ChatHealthyException(mode="runtime_error", message=f"specialty_classification_catalog: collection "
                f"{_DB_NAME}.{_COLL_NAME} returned 0 documents")
        _cache = out
        return _cache


def clear_cache() -> None:
    """Test-only: force the next load_catalog() to re-read the collection."""
    global _cache
    with _cache_lock:
        _cache = None
