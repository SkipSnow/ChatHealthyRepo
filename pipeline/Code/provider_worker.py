# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ProviderWorker — Step 4 raw-load worker.

Reads one byte-range slice of the NPI CSV from Azure Blob, applies the
state filter against the raw column values, and bulk-upserts a flat
``{npi, load_id, record_id, worker_id, raw: {original-column: value}}``
document to MongoDB. No normalization, no county crosswalk, no flag
stamping — those moves to Step 5 (normalize_provider_rows) and Step 12
(provider_flags_enrichment) so each step gets its own per-activity budget.

Idempotency: each doc is upserted on `npi` via ReplaceOne(..., upsert=True).
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


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(
            os.environ["MONGO_connectionString"],
            serverSelectionTimeoutMS=120_000,  # survive Atlas autoscale elections (~30-90s)
            maxPoolSize=64,
        )
    return _mongo


# Synthetic record IDs: worker_id * MAX_ROWS_PER_WORKER + local_row_index.
# NPI is the business key — record_id is for internal dedup only.
MAX_ROWS_PER_WORKER = 1_000_000

STATUS_LABELS = {
    0:  "New",
    10: "Load invoked",
    11: "Load failed",
    12: "Load succeeded",
}


def status_fields(code: int) -> dict:
    return {"status": code, "status_text": STATUS_LABELS.get(code, str(code))}


# ── Raw-row state filter ─────────────────────────────────────────────────────

BOUNDARY_EXTRA = 8192  # extra bytes read past end_byte to complete the last row

# Raw NPPES column names that carry US state codes. The Step-4 worker
# inspects these directly so it can apply the state filter BEFORE the row
# gets normalized into structured taxonomies/licenses/etc. (Step 5).
_PRACTICE_STATE_COL = "Provider Business Practice Location Address State Name"
_MAILING_STATE_COL  = "Provider Business Mailing Address State Name"
_LICENSE_STATE_PREFIX = "Provider License Number State Code_"
_OID_STATE_PREFIX     = "Other Provider Identifier State_"


def _raw_row_matches_state(raw_row: dict, states: list) -> bool:
    """Return True when any state-bearing raw column in `raw_row` matches `states`.

    Mirrors state_filter.STATE_BEARING_MONGO_FIELDS, but inspects raw NPPES
    column names instead of normalized Mongo paths so the filter applies
    BEFORE normalization.
    """
    states_set = {s.upper() for s in states}
    for col in (_PRACTICE_STATE_COL, _MAILING_STATE_COL):
        v = (raw_row.get(col) or "").strip().upper()
        if v in states_set:
            return True
    for k, v in raw_row.items():
        if not v:
            continue
        if k.startswith(_LICENSE_STATE_PREFIX) or k.startswith(_OID_STATE_PREFIX):
            vv = v.strip().upper()
            if vv in states_set:
                return True
    return False


def _iter_lines(stream, stop_after: int | None = None):
    """Yield decoded lines from a blob StorageStreamDownloader without loading all bytes."""
    buffer = b""
    bytes_consumed = 0
    for chunk in stream.chunks():
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            bytes_consumed += len(line) + 1
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

        from state_filter import normalize_states
        self._states = normalize_states(config)  # raises on missing/empty/malformed
        # ALL sentinel disables filtering — accept every row.
        from state_filter import is_full_load
        self._full_load = is_full_load(self._states)
        self._skipped_states = 0

        # ENH-PIPE-001: incremental mode — replace_one with upsert instead of insert.
        self._incremental = config.get("incremental", False)

        # State set in _pipeline_open().
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
        for row in self._reader:
            if not any(f.strip() for f in row):
                continue
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

        # Build raw dict directly from header+row; skip empty values to keep
        # the stored doc compact.
        raw = {h: v.strip() for h, v in zip(self.header, row) if v and v.strip()}

        if not self._full_load and not _raw_row_matches_state(raw, self._states):
            self._skipped_states += 1
            return

        npi = (raw.get("NPI") or "").strip()
        if not npi:
            raise ValueError(
                f"NPPES corruption: row missing NPI "
                f"(worker={self.worker_id}, local_index={self._local_index})"
            )

        record_id = self.worker_id * MAX_ROWS_PER_WORKER + self._local_index

        # loaded_at stamped here at parse time (Skip: "stamped by the
        # ChatHealthyDataPipelineProcessor") — required by the provider
        # schema, ISO-8601 UTC. Captures when our pipeline first touched
        # the record in this load.
        doc = {
            "npi":        npi,
            "load_id":    self.load_id,
            "record_id":  record_id,
            "loaded_at":  datetime.now(timezone.utc).isoformat(),
            "raw":        raw,
        }

        if self._incremental:
            # ENH-PIPE-001: force re-enrichment + re-evaluation on existing records.
            doc["bad_data"] = None
            doc["out_of_scope"] = None

            # ENH-PIPE-002 / PIPE-INC-002: deactivated rows in incremental files get
            # out_of_scope flagged instead of being deleted.
            if raw.get("NPI Deactivation Date") and not raw.get("NPI Reactivation Date"):
                doc["out_of_scope"] = {"flagged": True, "reason": "deactivated"}
                logging.info(
                    "ENH-PIPE-002: deactivated NPI=%s flagged out_of_scope (incremental)",
                    npi,
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
