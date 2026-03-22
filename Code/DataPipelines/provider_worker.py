# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""ProviderWorker — reads one byte-range slice of the NPI CSV from Azure Blob,
parses rows, normalizes multi-valued fields to arrays,
and batch-upserts to the MongoDB staging collection.

Idempotency: each document is upserted on (load_id, record_id).
The unique index on that pair is created by ensure_indexes_activity before
any workers start, so duplicate inserts on retry are silently ignored.
"""

import csv
import logging
import os
import time
from datetime import datetime, timezone

from blob_client import get_blob_service
from bson import ObjectId
from pymongo import MongoClient, UpdateOne

_mongo: MongoClient | None = None


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(
            os.environ["MONGO_connectionString"],
            serverSelectionTimeoutMS=120_000,  # survive Atlas autoscale elections (~30-90s)
        )
    return _mongo


# Synthetic record IDs: worker_id * MAX_ROWS_PER_WORKER + local_row_index.
# NPI is the business key — record_id is for internal dedup only.
# Workers are always dispatched in sufficient number to keep each under 1,000,000 rows.
# Max record_id = 200 * 1,000,000 = 200,000,000 — fits in a 32-bit signed int.
MAX_ROWS_PER_WORKER = 1_000_000

STATUS_LABELS = {
    0:  "New",
    10: "Load invoked",
    11: "Load failed",
    12: "Load succeeded",
}


def status_fields(code: int) -> dict:
    """Return status and status_text fields for a metadata update."""
    return {"status": code, "status_text": STATUS_LABELS.get(code, str(code))}


# ── Field normalization constants ─────────────────────────────────────────────

# Numbered field groups → collapsed into arrays
BOUNDARY_EXTRA = 8192  # extra bytes read past end_byte to complete the last row

TAX_CODE_PREFIX = "Healthcare Provider Taxonomy Code_"
TAX_SWITCH_PREFIX = "Healthcare Provider Primary Taxonomy Switch_"
TAX_GROUP_PREFIX = "Healthcare Provider Taxonomy Group_"

ARRAY_FIELD_GROUPS = [
    ("Provider License Number_", "license_numbers"),
    ("Provider License Number State Code_", "license_states"),
    ("Other Provider Identifier_", "other_identifiers"),
    ("Other Provider Identifier Type Code_", "other_identifier_types"),
    ("Other Provider Identifier State_", "other_identifier_states"),
    ("Other Provider Identifier Issuer_", "other_identifier_issuers"),
]

# Address fields → collapsed into sub-documents
PRACTICE_ADDRESS_FIELDS = {
    "Provider First Line Business Practice Location Address": "line1",
    "Provider Second Line Business Practice Location Address": "line2",
    "Provider Business Practice Location Address City Name": "city",
    "Provider Business Practice Location Address State Name": "state",
    "Provider Business Practice Location Address Postal Code": "zip",
    "Provider Business Practice Location Address Country Code (If outside U.S.)": "country",
    "Provider Business Practice Location Address Telephone Number": "phone",
    "Provider Business Practice Location Address Fax Number": "fax",
}

MAILING_ADDRESS_FIELDS = {
    "Provider First Line Business Mailing Address": "line1",
    "Provider Second Line Business Mailing Address": "line2",
    "Provider Business Mailing Address City Name": "city",
    "Provider Business Mailing Address State Name": "state",
    "Provider Business Mailing Address Postal Code": "zip",
    "Provider Business Mailing Address Country Code (If outside U.S.)": "country",
    "Provider Business Mailing Address Telephone Number": "phone",
    "Provider Business Mailing Address Fax Number": "fax",
}


def _normalize_row(header: list, row: list) -> dict:
    """Convert a flat NPI CSV row into a normalized MongoDB document."""
    raw = dict(zip(header, row))
    doc = {}
    consumed = set()

    # Collapse taxonomy parallel arrays into array of objects
    for prefix in (TAX_CODE_PREFIX, TAX_SWITCH_PREFIX, TAX_GROUP_PREFIX):
        consumed.update(h for h in header if h.startswith(prefix))
    taxonomies = []
    for h in sorted(h for h in header if h.startswith(TAX_CODE_PREFIX)):
        idx = h[len(TAX_CODE_PREFIX):]
        code = raw.get(h, "").strip()
        if not code:
            continue
        switch = raw.get(f"{TAX_SWITCH_PREFIX}{idx}", "").strip()
        group = raw.get(f"{TAX_GROUP_PREFIX}{idx}", "").strip()
        entry = {"code": code, "primary": switch.upper() == "Y"}
        if group:
            entry["group"] = group
        taxonomies.append(entry)
    if taxonomies:
        doc["taxonomies"] = taxonomies

    # Collapse numbered groups into arrays
    for prefix, key in ARRAY_FIELD_GROUPS:
        values = [
            raw[h].strip()
            for h in header
            if h.startswith(prefix) and raw.get(h, "").strip()
        ]
        consumed.update(h for h in header if h.startswith(prefix))
        if values:
            doc[key] = values

    # Collapse practice address fields into sub-document
    practice = {
        sub: raw[field].strip()
        for field, sub in PRACTICE_ADDRESS_FIELDS.items()
        if raw.get(field, "").strip()
    }
    consumed.update(PRACTICE_ADDRESS_FIELDS)
    if practice:
        doc["practice_address"] = practice

    # Collapse mailing address fields into sub-document
    mailing = {
        sub: raw[field].strip()
        for field, sub in MAILING_ADDRESS_FIELDS.items()
        if raw.get(field, "").strip()
    }
    consumed.update(MAILING_ADDRESS_FIELDS)
    if mailing:
        doc["mailing_address"] = mailing

    # Remaining scalar fields — snake_case the key, skip empty values
    for h, v in raw.items():
        if h in consumed:
            continue
        v = v.strip() if isinstance(v, str) else v
        if v:
            key = (
                h.lower()
                .replace(" ", "_")
                .replace("(", "")
                .replace(")", "")
                .replace("/", "_")
                .replace(".", "")
            )
            doc[key] = v

    return doc


def _iter_lines(stream, stop_after: int | None = None):
    """Yield decoded lines from a blob StorageStreamDownloader without loading all bytes.

    stop_after: if set, stop after the first complete line where cumulative bytes >= stop_after.
    This allows a worker to read slightly past its logical end_byte to complete the last row,
    without processing rows that belong to the next worker.
    """
    buffer = b""
    bytes_consumed = 0
    for chunk in stream.chunks():
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            bytes_consumed += len(line) + 1  # +1 for the \n
            yield line.decode("utf-8", errors="replace")
            if stop_after is not None and bytes_consumed >= stop_after:
                return
    if buffer:
        yield buffer.decode("utf-8", errors="replace")


class ProviderWorker:

    def __init__(self, config: dict):
        self.worker_id = config["worker_id"]
        self.start_byte = config["start_byte"]
        self.end_byte = config["end_byte"]
        self.header = config["header"]
        self.csv_path = config["csv_path"]
        self.load_id = config["load_id"]
        self.metadata_id = config["metadata_id"]
        self.batch_size = config.get("batch_size", 5000)
        self.staging_collection = config.get(
            "staging_collection", "PublicHealthData.providers_staging"
        )
        self.metadata_collection = config.get("metadata_collection", "admin.DataLoadMetadata")
        self.blob_container = config.get("blob_container", "provider-data")

    def run(self) -> dict:
        self._update_status(10, None)
        started_at = datetime.now(timezone.utc).isoformat()
        start_time = time.monotonic()
        try:
            rows_processed, num_records, rows_failed, failed_rows = self._load()
            duration = time.monotonic() - start_time
            finished_at = datetime.now(timezone.utc).isoformat()
            rows_per_second = round(rows_processed / duration, 1) if duration > 0 else 0
            self._update_status(12, None, num_records=num_records)
            logging.info(
                "Worker %d: processed=%d inserted=%d failed=%d %.1fs (%.1f rows/s)",
                self.worker_id, rows_processed, num_records, rows_failed,
                duration, rows_per_second,
            )
            return {
                "worker_id": self.worker_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "rows_processed": rows_processed,
                "num_records": num_records,
                "rows_failed": rows_failed,
                "failed_rows": failed_rows,
                "duration_seconds": round(duration, 2),
                "rows_per_second": rows_per_second,
                "success": True,
            }
        except Exception as exc:
            logging.exception("Worker %d failed", self.worker_id)
            self._update_status(11, str(exc))
            return {
                "worker_id": self.worker_id,
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "num_records": 0,
                "rows_failed": 0,
                "success": False,
                "error": str(exc),
            }

    def _load(self) -> tuple[int, int, int, list]:
        # Read byte range from blob
        service = get_blob_service()
        blob_client = (
            service.get_container_client(self.blob_container)
            .get_blob_client(self.csv_path)
        )
        logical_end = self.end_byte
        actual_end = self.end_byte + BOUNDARY_EXTRA
        length = actual_end - self.start_byte
        stop_after = logical_end - self.start_byte
        stream = blob_client.download_blob(offset=self.start_byte, length=length)

        db_name, coll_name = self.staging_collection.split(".", 1)
        collection = _get_mongo_client()[db_name][coll_name]

        batch = []
        local_index = 0
        num_records = 0
        rows_failed = 0
        failed_rows = []          # captured samples (max 20)
        is_first_row = True

        reader = csv.reader(_iter_lines(stream, stop_after=stop_after))
        for row in reader:
            if not any(f.strip() for f in row):   # skip blank/whitespace-only lines
                continue

            # Workers 2-N: partition boundaries are newline-aligned, so the
            # first row is normally complete. If the alignment fallback fired
            # (no \n found in scan window), the first bytes may be mid-row.
            # Discard that partial leading row silently — not counted as rows_failed.
            if is_first_row:
                is_first_row = False
                if self.worker_id > 1 and len(row) != len(self.header):
                    continue  # partial leading row — discard

            if len(row) != len(self.header):       # malformed non-first row
                rows_failed += 1
                entry = {
                    "row_number": local_index,
                    "field_count": len(row),
                    "expected": len(self.header),
                    "raw": ",".join(row[:20]),  # first 20 fields for identification
                }
                if len(failed_rows) < 20:
                    failed_rows.append(entry)
                else:
                    logging.error(
                        "Worker %d failed row overflow (row %d): fields=%d expected=%d raw=%.120s",
                        self.worker_id, local_index, len(row), len(self.header), entry["raw"],
                    )
                continue

            doc = _normalize_row(self.header, row)
            record_id = self.worker_id * MAX_ROWS_PER_WORKER + local_index
            doc["load_id"] = self.load_id
            doc["record_id"] = record_id
            doc["worker_id"] = self.worker_id
            doc["county"] = {"fips": None}  # stub — enriched by county_enrichment_job

            # Upsert on (load_id, record_id) — safe to retry; duplicate
            # inserts are ignored by the unique index created before workers start.
            batch.append(UpdateOne(
                {"load_id": self.load_id, "record_id": record_id},
                {"$setOnInsert": doc},
                upsert=True,
            ))
            local_index += 1
            num_records += 1

            if len(batch) >= self.batch_size:
                collection.bulk_write(batch, ordered=False)
                batch = []

        if batch:
            collection.bulk_write(batch, ordered=False)

        rows_processed = num_records + rows_failed
        return rows_processed, num_records, rows_failed, failed_rows

    def _update_status(
        self, status: int, error: str | None, num_records: int = 0
    ) -> None:
        db_name, coll_name = self.metadata_collection.split(".", 1)
        update: dict = {"$set": status_fields(status)}
        if error:
            update["$set"]["error_detail"] = error
        if num_records is not None:
            update["$set"]["num_records"] = num_records
        _get_mongo_client()[db_name][coll_name].update_one(
            {"_id": ObjectId(self.metadata_id)}, update
        )
