from chathealthy_lib.logging_service import ChatHealthyLoggingService
# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
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

Warning vs Error (use log_warning() and log_error())
-----------------------------------------------------
  ERROR   — Row cannot be published:
            - Row not loadable (parse failure, required field missing)
            - County not found after all enrichment step filter functions run
            Logged at ERROR level. Also written to DiscrepancyReport level="error".

  WARNING — Row is publishable but has data quality issues:
            - Enrichment data inconsistent or missing but row still usable
            - Optional field populated with default
            - Data quality flag raised but row can proceed
            - Any issue that does not prevent publishing
            Logged at INFO level. Also written to DiscrepancyReport level="warning".

  FATAL   — Job-terminating failure (MongoDB unreachable, vault unreachable, etc.).
            Only DiscrepancyReport.fatal_error() or automatic exception handling
            emit these. No log_fatal() method; let the exception propagate
            or call fatal_error() explicitly.
"""


import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

_base_mongo = None  # lazy singleton — avoids import at module load when MongoDB is unavailable


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
        # DiscrepancyReport for forking warnings/errors to both logs and discrepancy tracking
        self._discrepancy_report = self._initialize_discrepancy_report(config)
        # Threshold gates (read from config, checked against global counters in DB)
        self._warning_threshold: int | None = config.get("warning_threshold")
        self._error_threshold: int | None = config.get("error_threshold")

    def _initialize_discrepancy_report(self, config: dict):
        """Initialize DiscrepancyReport if config provides required fields."""
        try:
            from chathealthy_lib.discrepancy_report import DiscrepancyReport
            run_id = config.get("run_id")
            env = config.get("env", os.environ.get("ENV_PREFIX", "dev"))
            pipeline_name = config.get("job", "unknown")
            source = config.get("source", "pipeline")
            if not run_id:
                return None
            # data_version and the target collection come from config: the
            # worker knows which data generation it is operating on and which
            # collection it writes, and the report infers neither.
            #
            # The three counts are None deliberately. A worker reporting a
            # fatal mid-run has no business asserting how many rows the run
            # landed -- it is failing partway through one. None prints
            # Unknown, which is the true answer here; zero would be a lie.
            return DiscrepancyReport(
                run_id=run_id,
                env=env,
                pipeline_name=pipeline_name,
                source=source,
                target_collection=config.get("provider_collection", ""),
                total_source_rows=None,
                rows_in_target=None,
                total_rows=None,
                data_version=config.get("data_version"),
            )
        except Exception:
            return None

    # ── Public entry point ────────────────────────────────────────────────────

    def pipeline_execute(self) -> dict:
        """Drive the processing loop.  Called by orchestration."""
        fatal_exception = None
        try:
            self._pipeline_open()
            while self._pipeline_has_next():
                self._pipeline_step()
        except Exception as exc:
            self._job_failed = True
            fatal_exception = exc
            self._pipeline_on_job_failure(exc)
            raise
        finally:
            self._pipeline_close()
            self._write_row_errors(fatal_exception=fatal_exception)
        return self._pipeline_build_result()

    # ── Warning and error forking ──────────────────────────────────────────────

    def log_warning(self, explanation: str, source_line: str | None = None, npi: str | None = None) -> None:
        """Log a warning to both ChatHealthyLoggingService (info level) and DiscrepancyReport.
        If warning_threshold is exceeded, triggers fatal_error.

        Args:
            explanation: Warning explanation.
            source_line: Source location (e.g., row key, NPI).
            npi: Optional NPI.

        Raises:
            Exception if threshold exceeded (fatal_error called).
        """
        log = ChatHealthyLoggingService()
        if not self._discrepancy_report:
            log.info("Row warning [%s]: %s", source_line or "unknown", explanation)
            return

        # Increment global counter in database
        warning_count = self._discrepancy_report.increment_warning_count()
        log.info("Row warning [%d/%s] [%s]: %s",
                 warning_count,
                 self._warning_threshold or "unlimited",
                 source_line or "unknown",
                 explanation)

        # Write discrepancy
        self._discrepancy_report.write(
            discrepancy_level="warning",
            explanation=explanation,
            source_line=source_line,
            npi=npi,
            field=None,
        )

        # Check threshold
        if self._warning_threshold and warning_count >= self._warning_threshold:
            from chathealthy_lib.discrepancy_report import fatal_error
            fatal_error(
                self._discrepancy_report,
                level="fatal",
                explanation=f"Warning threshold exceeded: {warning_count} warnings >= {self._warning_threshold} limit",
                source_line=source_line,
                records_processed=warning_count,
                rows_before_fatal=warning_count,
            )
            raise Exception(f"Warning threshold exceeded: {warning_count} >= {self._warning_threshold}")

    def log_error(self, explanation: str, source_line: str | None = None, npi: str | None = None) -> None:
        """Log an error to both ChatHealthyLoggingService (error level) and DiscrepancyReport.
        If error_threshold is exceeded, triggers fatal_error.

        Args:
            explanation: Error explanation.
            source_line: Source location (e.g., row key, NPI).
            npi: Optional NPI.

        Raises:
            Exception if threshold exceeded (fatal_error called).
        """
        log = ChatHealthyLoggingService()
        if not self._discrepancy_report:
            log.error("Row error [%s]: %s", source_line or "unknown", explanation)
            return

        # Increment global counter in database
        error_count = self._discrepancy_report.increment_error_count()
        log.error("Row error [%d/%s] [%s]: %s",
                  error_count,
                  self._error_threshold or "unlimited",
                  source_line or "unknown",
                  explanation)

        # Write discrepancy
        self._discrepancy_report.write(
            discrepancy_level="error",
            explanation=explanation,
            source_line=source_line,
            npi=npi,
            field=None,
        )

        # Check threshold
        if self._error_threshold and error_count >= self._error_threshold:
            from chathealthy_lib.discrepancy_report import fatal_error
            fatal_error(
                self._discrepancy_report,
                level="fatal",
                explanation=f"Error threshold exceeded: {error_count} errors >= {self._error_threshold} limit",
                source_line=source_line,
                records_processed=error_count,
                rows_before_fatal=error_count,
            )
            raise Exception(f"Error threshold exceeded: {error_count} >= {self._error_threshold}")

    # ── Row-error persistence (internal) ─────────────────────────────────────

    def _write_row_errors(self, *, fatal_exception: Exception | None = None) -> None:
        """Write row_errors and fatal_exception to admin.PipelineDiscrepancyReports.

        Best-effort — logs a warning on failure but never raises.
        No-op when both row_errors is empty and fatal_exception is None, or
        when report_collection is not set in config.
        """
        if (not self.row_errors and not fatal_exception) or not self._report_collection:
            return
        try:
            client = None if not os.environ.get("MONGO_HOST") else ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")
            if client is None:
                ChatHealthyLoggingService().warning(
                    "PipelineWorkerBase: MONGO_HOST not set — "
                    "%d row errors + %s fatal exception not persisted",
                    len(self.row_errors), "no" if not fatal_exception else "a"
                )
                return
            db_name, coll_name = self._report_collection.split(".", 1)
            doc = {
                "job": self._job,
                "load_id": self._load_id,
                "worker_id": self._worker_id,
                "row_errors": self.row_errors,
                "datetime": datetime.now(timezone.utc).isoformat(),
            }
            if fatal_exception:
                doc["fatal_exception"] = {
                    "type": type(fatal_exception).__name__,
                    "message": str(fatal_exception),
                    "mode": getattr(fatal_exception, "mode", "unknown"),
                }
            client[db_name][coll_name].insert_one(doc)
            ChatHealthyLoggingService().info(
                "PipelineWorkerBase: wrote %d row errors%s to %s (load_id=%s)",
                len(self.row_errors),
                " + fatal exception" if fatal_exception else "",
                self._report_collection, self._load_id,
            )
        except Exception as exc:
            ChatHealthyLoggingService().warning("PipelineWorkerBase: failed to persist errors: %s", exc)

    # ── Template method (private) ─────────────────────────────────────────────

    def _pipeline_step(self) -> None:
        """Call _pipeline_process(); handle row-level exceptions per
        fail_on_row_error policy."""
        try:
            self._pipeline_process()
        except Exception as exc:
            key = self._pipeline_row_key()
            reason = str(exc)
            log = ChatHealthyLoggingService()

            if self._discrepancy_report:
                # Increment global counter in database
                error_count = self._discrepancy_report.increment_error_count()
                log.error("Row error [%d/%s] [%s]: %s",
                          error_count,
                          self._error_threshold or "unlimited",
                          key,
                          reason)
                # Write discrepancy
                self._discrepancy_report.write(
                    discrepancy_level="error",
                    explanation=reason,
                    source_line=key,
                    npi=None,
                    field=None,
                )
                # Check threshold
                if self._error_threshold and error_count >= self._error_threshold:
                    from chathealthy_lib.discrepancy_report import fatal_error
                    fatal_error(
                        self._discrepancy_report,
                        level="fatal",
                        explanation=f"Error threshold exceeded: {error_count} errors >= {self._error_threshold} limit",
                        source_line=key,
                        records_processed=error_count,
                        rows_before_fatal=error_count,
                    )
                    raise Exception(f"Error threshold exceeded: {error_count} >= {self._error_threshold}")
            else:
                log.error("Row error [%s]: %s", key, reason)

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

    # ── Idempotent resume helper ───────────────────────────────────────────────

    @staticmethod
    def output_exists_and_valid(collection, *, min_count: int = 1) -> bool:
        """Return True when *collection* already contains >= *min_count* docs.

        Used by orchestrators before launching a worker — if the output is
        already present and meets the minimum threshold the step can be
        skipped (idempotent resume).
        """
        try:
            count = collection.count_documents({})
            ok = count >= min_count
            if ok:
                ChatHealthyLoggingService().warning(
                    "output_exists_and_valid: %s has %d docs (>= %d) — skip",
                    getattr(collection, "full_name", "?"), count, min_count,
                )
            return ok
        except Exception as exc:
            ChatHealthyLoggingService().warning("output_exists_and_valid: check failed (%s)", exc)
            return False

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
