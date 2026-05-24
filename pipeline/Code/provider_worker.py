# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
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
from pipeline_worker_base import PipelineWorkerBase
from pymongo import MongoClient, ReplaceOne

_mongo: MongoClient | None = None
_crosswalk_cache: dict | None = None


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(
            os.environ["MONGO_connectionString"],
            serverSelectionTimeoutMS=120_000,  # survive Atlas autoscale elections (~30-90s)
            maxPoolSize=64,
        )
    return _mongo


def _get_crosswalk() -> dict:
    """Lazy-load ZIP → {fips, name, ratio, is_split} from ZipCountyCrosswalk.

    Lives at module scope so all workers in the same process share one cache.
    """
    global _crosswalk_cache
    if _crosswalk_cache is None:
        env_prefix = os.environ.get("ENV_PREFIX", "dev")
        coll = _get_mongo_client()[f"{env_prefix}_PublicHealthData"]["ZipCountyCrosswalk"]
        _crosswalk_cache = {
            d["zip"]: {
                "fips": d["county_fips"],
                "name": d["county_name"],
                "ratio": d.get("res_ratio"),
                "is_split": d.get("is_split", False),
            }
            for d in coll.find(
                {},
                {"zip": 1, "county_fips": 1, "county_name": 1, "res_ratio": 1, "is_split": 1},
            )
        }
        logging.info("Crosswalk cache: %d ZIPs loaded", len(_crosswalk_cache))
    return _crosswalk_cache


def _attach_initial_county(doc: dict, crosswalk: dict) -> None:
    """Stamp county on every practice_address element.

    Only stamps when the ZIP is in the crosswalk and the entry is not ambiguous
    (is_split=False). Ambiguous and missing ZIPs are left for Pass2 / Pass3
    (Census Geocoder) to resolve.

    County lives only at the per-element level (practice_address[].county).
    There is no top-level county field — readers must read per-element.
    """
    pa = doc.get("practice_address")
    if not isinstance(pa, list) or not pa:
        return
    for elem in pa:
        zip5 = (elem.get("zip") or "")[:5]
        if not zip5:
            continue
        entry = crosswalk.get(zip5)
        if not entry or entry["is_split"] or not entry["fips"]:
            continue
        elem["county"] = {
            "fips": entry["fips"],
            "name": entry["name"],
            "source": "crosswalk_load",
            "zip_ratio": entry["ratio"],
        }


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

BOUNDARY_EXTRA = 8192  # extra bytes read past end_byte to complete the last row

TAX_CODE_PREFIX = "Healthcare Provider Taxonomy Code_"
TAX_SWITCH_PREFIX = "Healthcare Provider Primary Taxonomy Switch_"
TAX_GROUP_PREFIX = "Healthcare Provider Taxonomy Group_"

LICENSE_NUMBER_PREFIX = "Provider License Number_"
LICENSE_STATE_PREFIX  = "Provider License Number State Code_"

ARRAY_FIELD_GROUPS = [
    ("Other Provider Identifier_", "other_identifiers"),
    ("Other Provider Identifier Type Code_", "other_identifier_types"),
    ("Other Provider Identifier State_", "other_identifier_states"),
    ("Other Provider Identifier Issuer_", "other_identifier_issuers"),
]

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
    """Convert a flat NPI CSV row into a normalized MongoDB document.

    `practice_address` is always emitted as a list with element 0 = primary
    practice location from the main NPPES file. Non-primary practice
    locations live in the NPPES Practice Location Reference File
    (`pl_pfile_*.csv`) and are attached separately by the parent pipeline's
    Step 5: Attaching secondary practice locations (server-side `$merge`).
    """
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

    # Collapse license parallel arrays into array of objects
    consumed.update(h for h in header if h.startswith(LICENSE_NUMBER_PREFIX))
    consumed.update(h for h in header if h.startswith(LICENSE_STATE_PREFIX))
    licenses = []
    for h in sorted(h for h in header if h.startswith(LICENSE_NUMBER_PREFIX)):
        idx = h[len(LICENSE_NUMBER_PREFIX):]
        number = raw.get(h, "").strip()
        state  = raw.get(f"{LICENSE_STATE_PREFIX}{idx}", "").strip()
        if not number and not state:
            continue
        entry = {}
        if state:
            entry["state"] = state
        if number:
            entry["number"] = number
        licenses.append(entry)
    if licenses:
        doc["licenses"] = licenses

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

    # Practice addresses are always a list. Element 0 is the primary practice
    # location from the main NPPES file (npidata_pfile). Secondary locations
    # are attached server-side in a later pipeline step from pl_pfile_*.csv.
    primary = {
        sub: raw[field].strip()
        for field, sub in PRACTICE_ADDRESS_FIELDS.items()
        if raw.get(field, "").strip()
    }
    consumed.update(PRACTICE_ADDRESS_FIELDS)
    if "zip" in primary:
        primary["zip"] = primary["zip"][:5]
    if primary:
        doc["practice_address"] = [primary]

    # Collapse mailing address fields into sub-document
    mailing = {
        sub: raw[field].strip()
        for field, sub in MAILING_ADDRESS_FIELDS.items()
        if raw.get(field, "").strip()
    }
    consumed.update(MAILING_ADDRESS_FIELDS)
    if "zip" in mailing:
        mailing["zip"] = mailing["zip"][:5]
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


class ProviderWorker(PipelineWorkerBase):

    def __init__(self, config: dict):
        super().__init__(config)
        self.worker_id = config["worker_id"]
        self.start_byte = config["start_byte"]
        self.end_byte = config["end_byte"]
        # header is fetched from the CSV blob in _pipeline_open() — not embedded
        # in per-partition config (kept the orchestrator outbox over the Durable
        # 45 KB largemessages spill threshold at 16+ workers).
        self.header: list = []
        self.csv_path = config["csv_path"]
        self.load_id = config["load_id"]
        self.metadata_id = config["metadata_id"]
        self.batch_size = config["batch_size"]
        self.provider_collection = config.get(
            "provider_collection", "dev_PublicHealthData.providers"
        )
        self.metadata_collection = config.get("metadata_collection", "admin.DataLoadMetadata")
        self.blob_container = config.get("blob_container", "provider-data")

        # State scope — uses shared state_filter helper so the row-level matcher
        # is identical to the Mongo predicate every other step uses
        # (EPIC-010-F-006-S-001 REQ-T-001 uniform predicate, REQ-T-002 raise on
        # missing, REQ-T-003 multi-state-bearing-field load, REQ-T-004 ALL sentinel).
        from state_filter import normalize_states
        self._states = normalize_states(config)  # raises on missing/empty/malformed
        self._skipped_states = 0

        # ENH-PIPE-001: incremental mode — replace_one with upsert instead of insert
        self._incremental = config.get("incremental", False)

        # State initialised in _pipeline_open(); set to safe defaults so
        # _pipeline_close() is always safe to call even if _pipeline_open()
        # did not complete.
        self._reader = None
        self._collection = None
        self._current_row: list | None = None
        self._npi_idx: int | None = None
        self._is_first_row: bool = True
        self._batch: list = []
        self._local_index: int = 0
        self._num_records: int = 0
        self._started_at: str = ""
        self._start_time: float = 0.0

    # ── PipelineWorkerBase overrides ──────────────────────────────────────────

    def _pipeline_open(self) -> None:
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._start_time = time.monotonic()
        self._update_status(10, None)

        service = get_blob_service()
        blob_client = (
            service.get_container_client(self.blob_container)
            .get_blob_client(self.csv_path)
        )

        # Fetch the CSV header from the first 32 KB of the blob (~12 KB header
        # for NPPES; 32 KB guarantees we capture the full first line).
        header_bytes = blob_client.download_blob(offset=0, length=32768).readall()
        header_text = header_bytes.decode("utf-8", errors="replace")
        if "\n" not in header_text:
            raise RuntimeError("CSV header line exceeds 32 KB — unexpected file format.")
        header_line = header_text.split("\n")[0]
        self.header = list(csv.reader([header_line]))[0]

        stop_after = self.end_byte - self.start_byte
        length = stop_after + BOUNDARY_EXTRA
        stream = blob_client.download_blob(offset=self.start_byte, length=length)
        self._reader = csv.reader(_iter_lines(stream, stop_after=stop_after))

        db_name, coll_name = self.provider_collection.split(".", 1)
        self._collection = _get_mongo_client()[db_name][coll_name]

        self._npi_idx = self.header.index("NPI") if "NPI" in self.header else None

    def _pipeline_has_next(self) -> bool:
        # Advance the iterator, silently skipping blank lines and the partial
        # leading row that can appear on non-first workers due to byte-range
        # boundary alignment.
        for row in self._reader:
            if not any(f.strip() for f in row):
                continue  # blank / whitespace-only line
            if self._is_first_row:
                self._is_first_row = False
                if self.worker_id > 1 and len(row) != len(self.header):
                    continue  # partial leading row — boundary alignment artifact
            self._current_row = row
            return True
        return False

    def _pipeline_process(self) -> None:
        row = self._current_row
        if len(row) != len(self.header):
            sample = ",".join(row[:10])
            raise ValueError(
                f"Malformed row: {len(row)} fields, expected {len(self.header)}, "
                f"sample={sample}"
            )

        doc = _normalize_row(self.header, row)

        # Multi-state-bearing-field row check — mirrors the Mongo $or predicate
        # used by drain/enrichment/embedding so load and drain stay symmetrical.
        from state_filter import doc_matches_state
        if not doc_matches_state(doc, self._states):
            self._skipped_states += 1
            return

        record_id = self.worker_id * MAX_ROWS_PER_WORKER + self._local_index
        doc["load_id"] = self.load_id
        doc["record_id"] = record_id
        doc["worker_id"] = self.worker_id
        # F-105: stamp can_prescribe and is_homeopathic with the placeholder
        # value True at load time so every record has the fields. The
        # apply_proprietary_flags activity overwrites these with the real
        # catalog-derived values; if that activity fails the pipeline fails
        # loud, so no record is ever silently left at the placeholder.
        # Indexes on these fields cover every record (no sparse-index gap).
        doc["can_prescribe"] = True
        doc["is_homeopathic"] = True
        _attach_initial_county(doc, _get_crosswalk())

        if self._incremental:
            # ENH-PIPE-001: replace_one with upsert — force re-enrichment and re-evaluation
            doc["bad_data"] = None       # force re-evaluation by mark_out_of_scope_fn
            doc["out_of_scope"] = None   # force re-evaluation by mark_out_of_scope_fn

            # ENH-PIPE-002 / PIPE-INC-002: deactivated records in incremental files
            # get out_of_scope flag instead of being deleted. Log NPI for audit.
            if doc.get("npi_deactivation_date") and not doc.get("npi_reactivation_date"):
                doc["out_of_scope"] = {"flagged": True, "reason": "deactivated"}
                logging.info(
                    "ENH-PIPE-002: deactivated NPI=%s flagged out_of_scope (incremental)",
                    doc.get("npi") or "unknown",
                )

        npi = doc.get("npi")
        if not npi:
            raise ValueError(
                f"NPPES corruption: row missing NPI "
                f"(worker={self.worker_id}, local_index={self._local_index})"
            )
        self._batch.append(ReplaceOne({"npi": npi}, doc, upsert=True))

        self._local_index += 1
        self._num_records += 1

        if len(self._batch) >= self.batch_size:
            self._collection.bulk_write(self._batch, ordered=False)
            self._batch = []

    def _pipeline_row_key(self) -> str:
        if self._current_row is not None and self._npi_idx is not None:
            try:
                return self._current_row[self._npi_idx]
            except IndexError:
                pass
        return f"worker_{self.worker_id}_idx_{self._local_index}"

    def _pipeline_resume(self) -> None:
        # _pipeline_has_next() already advanced the iterator to the next row.
        # The base class accumulates the error in row_errors.
        # No local state to reset — the batch only grows on successful
        # _pipeline_process() calls.
        pass

    def _pipeline_close(self) -> None:
        if self._batch and self._collection is not None:
            self._collection.bulk_write(self._batch, ordered=False)
            self._batch = []

    def _pipeline_on_job_failure(self, exc: Exception) -> None:
        self._update_status(11, str(exc))

    def _pipeline_build_result(self) -> dict:
        duration = time.monotonic() - self._start_time
        finished_at = datetime.now(timezone.utc).isoformat()
        rows_per_second = round(self._num_records / duration, 1) if duration > 0 else 0
        rows_processed = self._num_records + len(self.row_errors)

        self._update_status(12, None, num_records=self._num_records)
        logging.info(
            "Worker %d: processed=%d inserted=%d skipped_state=%d failed=%d %.1fs (%.1f rows/s)",
            self.worker_id, rows_processed, self._num_records, self._skipped_states,
            len(self.row_errors), duration, rows_per_second,
        )
        return {
            "worker_id": self.worker_id,
            "started_at": self._started_at,
            "finished_at": finished_at,
            "rows_processed": rows_processed,
            "num_records": self._num_records,
            "rows_failed": len(self.row_errors),
            "failed_rows": self.row_errors,
            "duration_seconds": round(duration, 2),
            "rows_per_second": rows_per_second,
            "success": True,
        }

    # ── Private ───────────────────────────────────────────────────────────────

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
