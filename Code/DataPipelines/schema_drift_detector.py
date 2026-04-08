# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pipeline Architecture v4 — SchemaDriftDetector
# Detects schema changes between pipeline runs by fingerprinting
# collection field names and types.

"""SchemaDriftDetector — computes and compares SHA-256 fingerprints of
MongoDB collection schemas to detect unexpected structural changes.

Fingerprints are stored in ``admin.SchemaFingerprints``.  On mismatch the
detector raises ``SchemaDriftError`` so the pipeline fails fast rather than
silently ingesting misshapen data.

Usage::

    detector = SchemaDriftDetector()
    result = detector.detect_and_alert(collection, "Providers", db_client)
    # result["passed"] is True when schema matches stored fingerprint

    # First run / after intentional migration:
    detector.store_fingerprint(collection, "Providers", db_client)

    # Hotfix bypass (skips raise, still logs warning):
    result = detector.detect_and_alert(collection, "Providers", db_client,
                                        allow_drift=True)
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("schema_drift_detector")

SAMPLE_SIZE = 100
FINGERPRINT_COLLECTION = "SchemaFingerprints"
FINGERPRINT_DB = "admin"


class SchemaDriftError(Exception):
    """Raised when the current collection schema does not match the stored
    fingerprint and ``allow_drift`` is False."""

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


class SchemaDriftDetector:
    """Compute and compare SHA-256 schema fingerprints for MongoDB collections.

    The fingerprint is built from the sorted, normalized set of
    ``(field_path, type_name)`` pairs observed across the first
    *SAMPLE_SIZE* documents.  Nested documents are flattened with
    dot-notation keys.
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def detect_and_alert(self, collection, collection_name: str,
                         db_client, *, allow_drift: bool = False) -> dict:
        """Compare live schema fingerprint against the stored value.

        Parameters
        ----------
        collection : pymongo.collection.Collection
            The collection to fingerprint.
        collection_name : str
            Logical name used as the key in ``admin.SchemaFingerprints``.
        db_client : pymongo.MongoClient
            Client with access to the ``admin`` database.
        allow_drift : bool
            When True, a mismatch is logged as a warning but does not raise.

        Returns
        -------
        dict
            ``{"passed": bool, "collection": str, "current_fingerprint": str,
              "stored_fingerprint": str | None}``

        Raises
        ------
        SchemaDriftError
            If the fingerprint differs and ``allow_drift`` is False.
        """
        current_fp = self._compute_fingerprint(collection)
        stored_fp = self._load_fingerprint(collection_name, db_client)

        result = {
            "passed": True,
            "collection": collection_name,
            "current_fingerprint": current_fp,
            "stored_fingerprint": stored_fp,
        }

        if stored_fp is None:
            log.info("SchemaDriftDetector: no stored fingerprint for '%s' — "
                     "treating as first run (passed)", collection_name)
            return result

        if current_fp != stored_fp:
            result["passed"] = False
            msg = (f"SchemaDriftDetector: schema drift detected for "
                   f"'{collection_name}' — stored={stored_fp[:12]}… "
                   f"current={current_fp[:12]}…")

            if allow_drift:
                log.warning("%s (allow_drift=True, continuing)", msg)
                return result

            log.error(msg)
            raise SchemaDriftError(msg, result)

        log.info("SchemaDriftDetector: fingerprint OK for '%s' (%s)",
                 collection_name, current_fp[:12])
        return result

    def store_fingerprint(self, collection, collection_name: str,
                          db_client) -> str:
        """Compute and persist the current schema fingerprint.

        Returns the fingerprint string.
        """
        fingerprint = self._compute_fingerprint(collection)
        fp_coll = db_client[FINGERPRINT_DB][FINGERPRINT_COLLECTION]
        fp_coll.update_one(
            {"collection_name": collection_name},
            {"$set": {
                "collection_name": collection_name,
                "fingerprint": fingerprint,
                "updated_utc": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )
        log.info("SchemaDriftDetector: stored fingerprint for '%s' (%s)",
                 collection_name, fingerprint[:12])
        return fingerprint

    # ── Internals ─────────────────────────────────────────────────────────────

    def _compute_fingerprint(self, collection) -> str:
        """Sample documents and produce a SHA-256 hex digest of the schema."""
        docs = list(collection.find({}, batch_size=SAMPLE_SIZE).limit(SAMPLE_SIZE))
        schema_pairs: set[tuple[str, str]] = set()
        for doc in docs:
            self._extract_fields(doc, "", schema_pairs)

        # Deterministic ordering → stable hash
        normalized = sorted(schema_pairs)
        payload = json.dumps(normalized, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _extract_fields(obj: dict, prefix: str,
                        out: set[tuple[str, str]]) -> None:
        """Recursively flatten *obj* into ``(dotted_key, type_name)`` pairs."""
        for key, value in obj.items():
            full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
            type_name = type(value).__name__
            out.add((full_key, type_name))
            if isinstance(value, dict):
                SchemaDriftDetector._extract_fields(value, full_key, out)

    @staticmethod
    def _load_fingerprint(collection_name: str, db_client) -> str | None:
        """Retrieve the stored fingerprint, or None if not yet recorded."""
        fp_coll = db_client[FINGERPRINT_DB][FINGERPRINT_COLLECTION]
        doc = fp_coll.find_one({"collection_name": collection_name})
        return doc["fingerprint"] if doc else None
