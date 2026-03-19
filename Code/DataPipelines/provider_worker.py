"""ProviderWorker — reads one byte-range slice of the NPI CSV from Azure Blob,
parses rows, normalizes multi-valued fields to arrays,
and batch-upserts to the MongoDB staging collection.

Idempotency: each document is upserted on (load_id, record_id).
The unique index on that pair is created by ensure_indexes_activity before
any workers start, so duplicate inserts on retry are silently ignored.
"""

import csv
import io
import logging
import os
import time

from azure.storage.blob import BlobServiceClient
from bson import ObjectId
from pymongo import MongoClient, UpdateOne

# Synthetic record IDs: worker_id * MAX_ROWS_PER_WORKER + local_row_index.
# NPI is the business key — record_id is for internal dedup only.
# Must be large enough that no worker can ever produce more rows than this.
MAX_ROWS_PER_WORKER = 1_000_000_000


# ── Field normalization constants ─────────────────────────────────────────────

# Numbered field groups → collapsed into arrays
ARRAY_FIELD_GROUPS = [
    ("Healthcare Provider Taxonomy Code_", "taxonomy_codes"),
    ("Healthcare Provider Primary Taxonomy Switch_", "taxonomy_primary_switches"),
    ("Healthcare Provider Taxonomy Group_", "taxonomy_groups"),
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


class ProviderWorker:

    def __init__(self, config: dict):
        self.worker_id = config["worker_id"]
        self.start_byte = config["start_byte"]
        self.end_byte = config["end_byte"]
        self.header = config["header"]
        self.csv_path = config["csv_path"]
        self.load_id = config["load_id"]
        self.metadata_id = config["metadata_id"]
        self.batch_size = config.get("batch_size", 500)
        self.staging_collection = config.get(
            "staging_collection", "PublicHealthData.providers_staging"
        )
        self.metadata_collection = config.get("metadata_collection", "admin.DataLoadMetadata")
        self.blob_container = config.get("blob_container", "provider-data")

    def run(self) -> dict:
        self._update_status(10, None)
        start_time = time.monotonic()
        try:
            num_records, rows_failed = self._load()
            duration = time.monotonic() - start_time
            rows_per_second = round(num_records / duration, 1) if duration > 0 else 0
            self._update_status(12, None, num_records=num_records)
            logging.info(
                "Worker %d loaded %d records in %.1fs (%.1f rows/s, %d failed)",
                self.worker_id, num_records, duration, rows_per_second, rows_failed,
            )
            return {
                "worker_id": self.worker_id,
                "num_records": num_records,
                "rows_failed": rows_failed,
                "duration_seconds": round(duration, 2),
                "rows_per_second": rows_per_second,
                "success": True,
            }
        except Exception as exc:
            logging.exception("Worker %d failed", self.worker_id)
            self._update_status(11, str(exc))
            return {
                "worker_id": self.worker_id,
                "num_records": 0,
                "rows_failed": 0,
                "success": False,
                "error": str(exc),
            }

    def _load(self) -> tuple[int, int]:
        # Read byte range from blob
        service = BlobServiceClient.from_connection_string(
            os.environ["AZURE_STORAGE_CONNECTION_STRING"]
        )
        blob_client = (
            service.get_container_client(self.blob_container)
            .get_blob_client(self.csv_path)
        )
        length = self.end_byte - self.start_byte
        raw_bytes = blob_client.download_blob(offset=self.start_byte, length=length).readall()
        content = raw_bytes.decode("utf-8", errors="replace")

        db_name, coll_name = self.staging_collection.split(".", 1)
        mongo_client = MongoClient(os.environ["MONGO_connectionString"])
        collection = mongo_client[db_name][coll_name]

        batch = []
        local_index = 0
        num_records = 0
        rows_failed = 0

        try:
            reader = csv.reader(io.StringIO(content))
            for row in reader:
                if not any(f.strip() for f in row):   # skip blank lines
                    continue
                if len(row) != len(self.header):       # skip malformed rows
                    rows_failed += 1
                    continue

                doc = _normalize_row(self.header, row)
                record_id = self.worker_id * MAX_ROWS_PER_WORKER + local_index
                doc["load_id"] = self.load_id
                doc["record_id"] = record_id
                doc["worker_id"] = self.worker_id

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
        finally:
            mongo_client.close()

        return num_records, rows_failed

    def _update_status(
        self, status: int, error: str | None, num_records: int = 0
    ) -> None:
        db_name, coll_name = self.metadata_collection.split(".", 1)
        client = MongoClient(os.environ["MONGO_connectionString"])
        try:
            update: dict = {"$set": {"status": status}}
            if error:
                update["$set"]["error_detail"] = error
            if num_records:
                update["$set"]["num_records"] = num_records
            client[db_name][coll_name].update_one(
                {"_id": ObjectId(self.metadata_id)}, update
            )
        finally:
            client.close()
