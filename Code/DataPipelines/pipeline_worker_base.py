# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""PipelineWorkerBase — Gang of Four Template Method base class for all
pipeline row-processing workers.

The base class owns the processing loop, all exception handling, and the
row_errors accumulator.  Subclasses own resource setup/teardown, row
iteration, row processing, and result construction.

None of the pipeline_ methods below are mandated by Azure or any external
framework.  They are internal to this class hierarchy only.

Subclass contract
-----------------
- Call super().__init__(config) from __init__.
- Implement every @abstractmethod.
- _pipeline_process(), _pipeline_has_next(), _pipeline_row_key(), and
  _pipeline_resume() MUST NOT catch exceptions or log errors — the base
  class owns all error handling and logging for the row lifecycle.
- _pipeline_open() and _pipeline_close() MAY perform resource setup/teardown
  but MUST NOT swallow exceptions.  _pipeline_close() is always called
  (success or failure) and must be safe to call even if _pipeline_open()
  did not complete.
- _pipeline_on_job_failure() is called exactly once on a fatal exception;
  override it for cleanup such as status-code writes.  Do not re-raise
  inside it.

fail_on_row_error (bool, default False)
---------------------------------------
  False → row errors are logged and appended to row_errors;
          _pipeline_resume() is called so the subclass can advance state;
          the loop continues.
  True  → the first row error abends the job (re-raises after logging).
"""

import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone

_base_mongo = None  # lazy singleton — avoids import at module load when MongoDB is unavailable


def _get_base_mongo_client():
    global _base_mongo
    if _base_mongo is None:
        conn_str = os.environ.get("MONGO_connectionString")
        if not conn_str:
            return None
        from pymongo import MongoClient
        _base_mongo = MongoClient(conn_str)
    return _base_mongo


class PipelineWorkerBase(ABC):

    def __init__(self, config: dict) -> None:
        self.fail_on_row_error: bool = config.get("fail_on_row_error", False)
        self.row_errors: list[dict] = []
        self._job_failed: bool = False
        # Optional — base class writes row_errors to MongoDB when these are set.
        self._report_collection: str | None = config.get("report_collection")
        self._load_id: str | None = config.get("load_id")
        self._job: str | None = config.get("job")
        self._worker_id: str | None = config.get("worker_id")

    # ── Public entry point ────────────────────────────────────────────────────

    def pipeline_execute(self) -> dict:
        """Drive the processing loop.  Called by orchestration."""
        try:
            self._pipeline_open()
            while self._pipeline_has_next():
                self._pipeline_step()
        except Exception as exc:
            self._job_failed = True
            self._pipeline_on_job_failure(exc)
            raise
        finally:
            self._pipeline_close()
            self._write_row_errors()
        return self._pipeline_build_result()

    # ── Row-error persistence (internal) ─────────────────────────────────────

    def _write_row_errors(self) -> None:
        """Write row_errors to admin.PipelineDiscrepancyReports if configured.

        Best-effort — logs a warning on failure but never raises.
        No-op when row_errors is empty or report_collection is not set in config.
        """
        if not self.row_errors or not self._report_collection:
            return
        try:
            client = _get_base_mongo_client()
            if client is None:
                logging.warning(
                    "PipelineWorkerBase: MONGO_connectionString not set — "
                    "%d row errors not persisted", len(self.row_errors)
                )
                return
            db_name, coll_name = self._report_collection.split(".", 1)
            client[db_name][coll_name].insert_one({
                "job": self._job,
                "load_id": self._load_id,
                "worker_id": self._worker_id,
                "row_errors": self.row_errors,
                "datetime": datetime.now(timezone.utc).isoformat(),
            })
            logging.info(
                "PipelineWorkerBase: wrote %d row errors to %s (load_id=%s)",
                len(self.row_errors), self._report_collection, self._load_id,
            )
        except Exception as exc:
            logging.warning("PipelineWorkerBase: failed to persist row errors: %s", exc)

    # ── Template method (private) ─────────────────────────────────────────────

    def _pipeline_step(self) -> None:
        """Call _pipeline_process(); handle row-level exceptions per
        fail_on_row_error policy."""
        try:
            self._pipeline_process()
        except Exception as exc:
            key = self._pipeline_row_key()
            reason = str(exc)
            logging.error("Row error [%s]: %s", key, reason)
            self.row_errors.append({"row_key": key, "reason": reason})
            if self.fail_on_row_error:
                raise
            self._pipeline_resume()

    # ── Optional hooks ────────────────────────────────────────────────────────

    def _pipeline_open(self) -> None:
        """Set up resources before the processing loop.  Override if needed."""

    def _pipeline_close(self) -> None:
        """Release/flush resources.  Always called — on success and on failure.
        Must be safe to call even if _pipeline_open() did not complete."""

    def _pipeline_on_job_failure(self, exc: Exception) -> None:
        """Called once when the job fails fatally.  Override for cleanup
        (e.g., status-code writes).  Do not re-raise inside this method."""

    # ── Abstract — subclasses must implement ──────────────────────────────────

    @abstractmethod
    def _pipeline_process(self) -> None:
        """Process the current row/item.  Raise on error.
        Do not catch exceptions or log errors here."""

    @abstractmethod
    def _pipeline_row_key(self) -> str:
        """Return the identifier for the current row (e.g., NPI).
        Called immediately after _pipeline_process() raises."""

    @abstractmethod
    def _pipeline_resume(self) -> None:
        """Advance past the failed row and reset any partial state.
        Do not raise from this method."""

    @abstractmethod
    def _pipeline_has_next(self) -> bool:
        """Advance the internal cursor and return True if a row is ready."""

    @abstractmethod
    def _pipeline_build_result(self) -> dict:
        """Build and return the result dict for the orchestrator.
        Called only on success, after _pipeline_close()."""
