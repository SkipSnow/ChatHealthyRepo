# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Discrepancy reporting for pipeline operations.

Discrepancies (warnings/errors) are stored on the FRONTEND cluster in the
`Pipelines` database -- the single home for all pipeline metadata.
Workers write discrepancies via DiscrepancyReport.write(); fatal errors are
emitted via DiscrepancyReport.emit() and fatal_error().
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from enum import Enum

from chathealthy_lib.logging_service import (
    ChatHealthyLoggingService,
    set_data_version,
    set_fatal_error,
    set_run_id,
)
from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

# Every pipeline reports through this one service, so its metadata home is
# stated here and imported by the pipelines rather than restated by each.
PIPELINE_ADMIN_DB = "pipelineAdmin"

# All discrepancy metadata lives in one collection in one database.
DISCREPANCIES_COLLECTION = "pipeline.discrepancy_reports"
VALID_EMAIL_SUFFIXES = (".com", ".ai", ".org", ".net", ".edu", ".gov")
RUN_COUNTERS_COLLECTION = "pipeline.run_counters"
PIPELINE_CONFIG_COLLECTION = "PipelineConfig"

_log = ChatHealthyLoggingService()


class FatalErrorReason(str, Enum):
    """Fatal error classifications."""
    MONGO_UNREACHABLE = "mongo_unreachable"
    VAULT_UNREACHABLE = "vault_unreachable"


class DiscrepancyDetail(str, Enum):
    """Discrepancy severity level.

    ERROR (row blocking):
      - Row cannot be loaded/parsed (parse failure, required field missing)
      - County not found after all enrichment step filter functions have run

    WARNING (non-blocking):
      - Enrichment data inconsistent or missing but row is still usable
      - Optional field populated with default
      - Data quality flag raised but row can proceed
      - Any issue that does not prevent the row from being published

    FATAL:
      - Job-terminating failure (MongoDB unreachable, vault unreachable, etc.)
    """
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


def _reject_bad_report_inputs(
    *, run_id: str, pipeline_name: str, target_collection: str,
    total_source_rows, rows_in_target, total_rows,
) -> None:
    """Raise-only: the catcher logs, not the thrower.

    Objected to on the way in, not discovered in the artifact. A collection
    name without its database addresses nothing, and a count that is neither a
    number nor a deliberate None is a caller that has not decided.
    """
    if not isinstance(target_collection, str) or "." not in target_collection:
        raise ChatHealthyException(
            mode="discrepancy_report_target_collection_invalid",
            component="DiscrepancyReport",
            message=("DiscrepancyReport: target_collection must be "
                     f"'<db>.<collection>'; got {target_collection!r}."),
            run_id=run_id, pipeline_name=pipeline_name,
        )
    for name, value in (("total_source_rows", total_source_rows),
                        ("rows_in_target", rows_in_target),
                        ("total_rows", total_rows)):
        if value is not None and not isinstance(value, int):
            raise ChatHealthyException(
                mode="discrepancy_report_count_invalid",
                component="DiscrepancyReport",
                message=(f"DiscrepancyReport: {name} must be an int, or None "
                         f"where the caller has no count; got {value!r}."),
                run_id=run_id, pipeline_name=pipeline_name,
            )


class DiscrepancyReport:
    """Emits discrepancy reports. Manages its own mongo connection."""

    def __init__(
        self,
        *,
        run_id: str,
        env: str,
        pipeline_name: str,
        source: str,
        total_source_rows: int | None,
        rows_in_target: int | None,
        total_rows: int | None,
        target_collection: str,
        fatal_error: bool = False,
        data_version: int | None = None,
    ) -> None:
        """
        Args:
            total_source_rows: rows the source offered this run.
            rows_in_target: rows this run put in the target collection.
            total_rows: rows the target collection holds in total.
            target_collection: "<db>.<collection>" this pipeline publishes to,
                printed in the header. Not queried.

        All four are REQUIRED and carry no default. A default would let a
        caller say nothing by accident and get Unknown without ever deciding
        to; every pipeline is made to state its numbers, and to pass None
        deliberately where it does not have one. target_collection must name
        its database, because a collection name without one addresses nothing.

        The three counts are the CLIENT's to supply, and this object never
        goes looking for them. Only the client knows what they mean for its
        own pipeline: source rows are a staging collection here, and could be
        a file's lines, an API's pages, or a sum across collections in the
        next pipeline. A library that counted them would need a branch per
        pipeline, and each branch would be a fresh chance to count the wrong
        thing against the wrong cluster.

        A count the client does not know is passed as None and reported as
        "Unknown". An absent count is never rendered as zero, and a count of
        zero is therefore an assertion the client is making rather than a
        failure to look.
        """
        _reject_bad_report_inputs(
            run_id=run_id, pipeline_name=pipeline_name,
            target_collection=target_collection,
            total_source_rows=total_source_rows,
            rows_in_target=rows_in_target, total_rows=total_rows,
        )
        self.run_id = run_id
        self.env = env
        self.pipeline_name = pipeline_name
        self.source = source
        self.total_source_rows = total_source_rows
        self.rows_in_target = rows_in_target
        self.total_rows = total_rows
        self.fatal_error = fatal_error
        self.data_version = data_version
        # What the run actually ended as. Without it the report cannot tell a
        # successful run from a failed one and describes every run as fatal.
        self.manifest_status = ""
        # Named by the caller, which knows it. Nothing else reliably does.
        self.target_collection = target_collection or ""
        # Bound FIRST, before any work that logs. The vault fetch, the
        # connection and the config load all emit records on the way in, and
        # anything bound after them produces records that belong to this run
        # but cannot be found by it.
        set_run_id(run_id)
        set_fatal_error(self.fatal_error)
        set_data_version(self.data_version)
        self.start_time = datetime.now(timezone.utc).isoformat()
        self.operator_email = self._get_operator_email_from_vault()
        # The exception this report exists to deliver. Held here rather than
        # fetched back out of Mongo, so the report is complete whether or not
        # the database is reachable.
        self.fatal_exception: ChatHealthyException | None = None
        self.mongo_down = False
        # What this process was told, kept where nothing outside it can fail.
        # The database is authoritative while it answers; when it stops, these
        # carry the run so no discrepancy and no count is lost from the report.
        self._held: list[dict] = []
        self._held_warnings = 0
        self._held_errors = 0

        # Discrepancy reports, run counters and pipeline config are all
        # metadata: admin target, reachable while the data factory is down.
        # "Down" is a state this object records and carries, never one it
        # raises on: a database that cannot be reached must not stop the
        # report that says so.
        self.mongo_connection = None
        self.config = {}
        try:
            self.mongo_connection = ChatHealthyMongoUtilities().getConnection(
                "pipelineEditor", "ChatHealthyFrontEnd")
            self.config = self._load_pipeline_config()
        except Exception as exc:  # noqa: BLE001 -- see above
            self.mongo_down = True
            self.mongo_down_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            _log.warning("discrepancy report: metadata database unreachable (%s); "
                         "the report proceeds without it", self.mongo_down_reason)
        _log.info("discrepancy report opened: pipeline=%s source=%s mongo_down=%s",
                  self.pipeline_name, self.source, self.mongo_down)

    def __del__(self) -> None:
        """The class has no finally of its own, so teardown lands here.

        Runs at garbage collection, which is non-deterministic and may occur
        during interpreter shutdown when logging handlers are already torn
        down. Everything is swallowed: a destructor that raises produces an
        ignored exception on stderr and helps nobody.
        """
        try:
            _log.info("discrepancy report closed: pipeline=%s", self.pipeline_name)
        except Exception:
            pass

    def _load_pipeline_config(self) -> dict:
        """Load pipeline configuration from the metadata database."""
        config = self.mongo_connection[PIPELINE_ADMIN_DB][
            PIPELINE_CONFIG_COLLECTION].find_one({"_id": self.pipeline_name})
        if not config:
            raise ChatHealthyException(
                mode="config_error",
                message=(
                    f"no {PIPELINE_CONFIG_COLLECTION} document for "
                    f"{self.pipeline_name!r} in {PIPELINE_ADMIN_DB}"
                ),
                component="DiscrepancyReport",
            )
        return config

    def _get_operator_email_from_vault(self) -> str | None:
        """Fetch operator email from Key Vault.

        The Azure SDK is imported here rather than at module scope. This is the
        only place the report uses it, and the report has to be importable
        where the SDK is absent -- the Automation sandbox carries pymongo,
        dnspython, certifi and reportlab, and nothing else.
        """
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            vault_uri = os.environ.get("KEY_VAULT_URI", "").strip()
            if not vault_uri:
                return None
            credential = DefaultAzureCredential()
            client = SecretClient(vault_uri=vault_uri, credential=credential)
            secret = client.get_secret("notification-to-email")
            return (secret.value or "").strip() if secret else None
        except Exception:
            return None

    def fatal(self, exc: ChatHealthyException) -> bool:
        """Report a fatal error the caller has constructed but not raised.

        The caller may be inside an except block or at a bare failure site;
        either way it builds the exception and hands it here rather than
        throwing. Nothing about this path needs the database: the exception
        already carries everything the report must say.

        This has exactly two ways to fail, and both raise: the mail server
        cannot be reached, and there is no valid address to send to.
        """
        self.fatal_exception = exc
        self.fatal_error = True
        set_fatal_error(True)
        _log.error("FATAL %s: %s", self.pipeline_name, self._fatal_explanation(),
                   extra={"fatal_error": True})
        self._record_fatal_in_mongo()
        return self._write_email()

    def _fatal_explanation(self) -> str:
        """One line naming what went fatal, from the exception in hand."""
        exc = self.fatal_exception
        if exc is None:
            return "fatal error reported without an exception"
        mode = getattr(exc, "mode", "") or ""
        message = str(getattr(exc, "message", "") or exc)
        return f"{mode}: {message}" if mode else message

    def _record_fatal_in_mongo(self) -> None:
        """Persist the fatal error when the database is there.

        A failure here flags the database down and returns. It is not the
        report's purpose to write to Mongo; it is the report's purpose to
        reach the operator.
        """
        if self.mongo_down or self.mongo_connection is None:
            return
        try:
            self.mongo_connection[PIPELINE_ADMIN_DB][DISCREPANCIES_COLLECTION].insert_one({
                "run_id": self.run_id,
                "job_name": self.pipeline_name,
                "source": self.source,
                "level": DiscrepancyDetail.FATAL.value,
                "explanation": self._fatal_explanation(),
            })
        except Exception as exc:  # noqa: BLE001
            self.mongo_down = True
            self.mongo_down_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            _log.warning("discrepancy report: could not persist the fatal error (%s); "
                         "the report still goes out", self.mongo_down_reason)

    def _hold_infrastructure_failure(self, what: str, reason: str) -> None:
        """Put a failure of the reporting machinery into the report itself.

        A database that will not take a discrepancy is a defect of the run and
        belongs on the artifact beside the data defects. Held once per distinct
        failure so a dead database produces one row rather than one per write.
        """
        explanation = f"{what}: {reason}"
        for row in self._held:
            if row.get("explanation") == explanation:
                return
        self._held.append(self._build_discrepancy_dict(
            DiscrepancyDetail.ERROR, explanation, None, None, None))

    @staticmethod
    def _identity(row: dict) -> tuple:
        """What makes a discrepancy the same discrepancy."""
        return (str(row.get("level", "")).lower(), row.get("npi"),
                row.get("source_line"), row.get("explanation"))

    def _counts(self) -> tuple:
        """Warning and error counts.

        The database counts the whole run across every worker. When it cannot
        be reached, this process reports what it recorded itself, and says
        Unknown only when it recorded nothing -- a count it does not have is
        never rendered as zero.
        """
        if not (self.mongo_down or self.mongo_connection is None):
            try:
                coll = self.mongo_connection[PIPELINE_ADMIN_DB][DISCREPANCIES_COLLECTION]
                return (coll.count_documents({"run_id": self.run_id, "level": "warning"}),
                        coll.count_documents({"run_id": self.run_id, "level": "error"}))
            except Exception as exc:  # noqa: BLE001
                self.mongo_down = True
                self.mongo_down_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
        return (self._held_warnings or "Unknown", self._held_errors or "Unknown")

    def _target_collection_name(self) -> str:
        """Short name of the collection this pipeline loads into. Never the
        staging collection."""
        if not self.target_collection:
            return "target collection"
        return str(self.target_collection).split(".")[-1]

    @staticmethod
    def _reported(count: int | None):
        """A count the caller supplied, or "Unknown" if it did not."""
        return "Unknown" if count is None else count

    def _build_manifest_doc(self, discrepancy_level: DiscrepancyDetail, explanation: str) -> dict:
        """Build manifest_doc from discrepancy details."""
        status = "fatal" if discrepancy_level == DiscrepancyDetail.FATAL else "failed" if discrepancy_level == DiscrepancyDetail.ERROR else "completed"
        doc = {
            "run_id": self.run_id,
            "pipeline_name": self.pipeline_name,
            "status": status,
        }
        if discrepancy_level == DiscrepancyDetail.FATAL:
            doc["abort_reason"] = explanation
            doc["fatal_reason"] = explanation
        return doc

    def _build_discrepancy_dict(
        self,
        discrepancy_level: str | DiscrepancyDetail,
        explanation: str,
        source_line: str | None = None,
        npi: str | None = None,
        field: str | None = None,
    ) -> dict:
        """Build discrepancy document for mongo."""
        if isinstance(discrepancy_level, str):
            level_enum = DiscrepancyDetail(discrepancy_level)
        else:
            level_enum = discrepancy_level

        return {
            "run_id": self.run_id,
            "level": level_enum.value,
            "source_line": source_line,
            "npi": npi,
            "field": field,
            "explanation": explanation,
        }

    def write(
        self,
        discrepancy_level: str | DiscrepancyDetail,
        explanation: str,
        source_line: str | None = None,
        npi: str | None = None,
        field: str | None = None,
    ) -> bool:
        """Write a discrepancy. Called by workers.

        Args:
            discrepancy_level: Severity level (warning, error, fatal).
            explanation: Error explanation.
            source_line: Source location.
            npi: Optional NPI.
            field: Optional field name.

        Returns:
            True if written successfully, False otherwise.
        """
        log = ChatHealthyLoggingService()
        level_enum = (DiscrepancyDetail(discrepancy_level)
                      if isinstance(discrepancy_level, str) else discrepancy_level)
        disc_dict = self._build_discrepancy_dict(
            level_enum, explanation, source_line, npi, field)
        # Held first. Whatever the database does next, the report can say this.
        self._held.append(disc_dict)

        if self.mongo_down or self.mongo_connection is None:
            return False
        try:
            self.mongo_connection[PIPELINE_ADMIN_DB][
                DISCREPANCIES_COLLECTION].insert_one(dict(disc_dict))
            return True
        except Exception as exc:  # noqa: BLE001
            self.mongo_down = True
            self.mongo_down_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            self._hold_infrastructure_failure(
                "the metadata database would not accept a discrepancy",
                self.mongo_down_reason)
            log.warning("discrepancy report: could not persist a %s (%s); "
                        "it is held for the report",
                        level_enum.value, self.mongo_down_reason)
            return False

    def increment_warning_count(self) -> int:
        """Atomically increment global warning counter for this run_id.
        Returns the new count after increment.
        """
        log = ChatHealthyLoggingService()
        self._held_warnings += 1
        if self.mongo_down or self.mongo_connection is None:
            return self._held_warnings
        try:
            counters_coll = self.mongo_connection[PIPELINE_ADMIN_DB][RUN_COUNTERS_COLLECTION]
            result = counters_coll.find_one_and_update(
                {"run_id": self.run_id},
                {"$inc": {"warning_count": 1}},
                upsert=True,
                return_document=True,
            )
            return result.get("warning_count", self._held_warnings)
        except Exception as exc:  # noqa: BLE001
            self.mongo_down = True
            self.mongo_down_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            self._hold_infrastructure_failure(
                "the metadata database would not increment the run warning counter",
                self.mongo_down_reason)
            log.warning("discrepancy report: could not increment the run warning "
                        "counter (%s); counting in memory", self.mongo_down_reason)
            return self._held_warnings

    def increment_error_count(self) -> int:
        """Atomically increment global error counter for this run_id.
        Returns the new count after increment.
        """
        log = ChatHealthyLoggingService()
        self._held_errors += 1
        if self.mongo_down or self.mongo_connection is None:
            return self._held_errors
        try:
            counters_coll = self.mongo_connection[PIPELINE_ADMIN_DB][RUN_COUNTERS_COLLECTION]
            result = counters_coll.find_one_and_update(
                {"run_id": self.run_id},
                {"$inc": {"error_count": 1}},
                upsert=True,
                return_document=True,
            )
            return result.get("error_count", self._held_errors)
        except Exception as exc:  # noqa: BLE001
            self.mongo_down = True
            self.mongo_down_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            self._hold_infrastructure_failure(
                "the metadata database would not increment the run error counter",
                self.mongo_down_reason)
            log.warning("discrepancy report: could not increment the run error "
                        "counter (%s); counting in memory", self.mongo_down_reason)
            return self._held_errors

    def get_counts(self) -> dict:
        """Get current warning and error counts for this run_id.
        Returns: {"warning_count": N, "error_count": M}
        """
        log = ChatHealthyLoggingService()
        held = {"warning_count": self._held_warnings,
                "error_count": self._held_errors}
        if self.mongo_down or self.mongo_connection is None:
            return held
        try:
            counters_coll = self.mongo_connection[PIPELINE_ADMIN_DB][RUN_COUNTERS_COLLECTION]
            result = counters_coll.find_one({"run_id": self.run_id})
            if not result:
                return held
            return {
                "warning_count": result.get("warning_count", self._held_warnings),
                "error_count": result.get("error_count", self._held_errors),
            }
        except Exception as exc:  # noqa: BLE001
            self.mongo_down = True
            self.mongo_down_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            self._hold_infrastructure_failure(
                "the metadata database would not return the run counters",
                self.mongo_down_reason)
            log.warning("discrepancy report: could not read the run counters (%s); "
                        "reporting what this process recorded",
                        self.mongo_down_reason)
            return held

    def _write_email(self) -> bool:
        """Send the fatal report. The only thing this method may not survive.

        Two failure modes, both raised:
          1. the mail server cannot be reached
          2. there is no valid address to send to

        Everything else -- an unreachable database, absent counts, missing
        prior discrepancies -- changes what the mail says, never whether it
        is sent. The fatal error itself is held in memory, so a report can be
        delivered by a process that has lost everything except its network.
        """
        warning_count, error_count = self._counts()

        # Prior discrepancies enrich the attachment. Their absence is not a
        # reason to stay silent about a fatal error.
        fatal_reports, error_reports, warning_reports = [], [], []
        if not self.mongo_down and self.mongo_connection is not None:
            try:
                coll = self.mongo_connection[PIPELINE_ADMIN_DB][DISCREPANCIES_COLLECTION]
                for row in coll.find({"run_id": self.run_id}):
                    level = str(row.get("level", "")).lower()
                    if level == "fatal":
                        fatal_reports.append(row)
                    elif level == "error":
                        error_reports.append(row)
                    elif level == "warning":
                        warning_reports.append(row)
            except Exception as exc:  # noqa: BLE001
                self.mongo_down = True
                self.mongo_down_reason = f"{type(exc).__name__}: {str(exc)[:200]}"

        # Anything this process was told but could not persist. Matched on the
        # row itself so a discrepancy that did reach the database is not
        # reported twice.
        persisted = {self._identity(r) for r in
                     fatal_reports + error_reports + warning_reports}
        for row in self._held:
            if self._identity(row) in persisted:
                continue
            level = str(row.get("level", "")).lower()
            if level == "fatal":
                fatal_reports.append(row)
            elif level == "error":
                error_reports.append(row)
            elif level == "warning":
                warning_reports.append(row)

        # A run that did not fail has no fatal error to report. This used to
        # synthesise one whenever none had been recorded, so every successful
        # run published a report whose headline was a fatal error that never
        # happened, explained as "reported without an exception" -- which was
        # the truth about the report, not about the run.
        is_fatal = bool(self.fatal_error or self.fatal_exception) or (
            self.manifest_status not in ("", "succeeded", "completed"))
        explanation = self._fatal_explanation() if is_fatal else ""
        if is_fatal and not fatal_reports:
            fatal_reports = [{
                "run_id": self.run_id,
                "job_name": self.pipeline_name,
                "source": self.source,
                "level": DiscrepancyDetail.FATAL.value,
                "explanation": explanation,
            }]

        end_time = datetime.now(timezone.utc).isoformat()
        manifest = {
            "run_id": self.run_id,
            "pipeline_name": self.pipeline_name,
            # The outcome, said outright. A reader had to infer it from the
            # absence of a fatal line.
            "run_status": self.manifest_status or ("failed" if is_fatal else "succeeded"),
            "run_started_utc": self.start_time,
            "run_ended_utc": end_time,
            "fatal_reason": explanation,
            "records_100_percent_successfully_collected": (
                not is_fatal and warning_count == 0 and error_count == 0),
            "records_with_non_fatal_warnings": warning_count,
            "records_with_non_fatal_errors": error_count,
            "rows_in_target": self._reported(self.rows_in_target),
            "target_collection": self._target_collection_name(),
            "total_rows": self._reported(self.total_rows),
            # Raw, never _reported: the header subtracts from this one to get
            # "records 100% successfully collected", and states its own absence
            # as Unknown when it is None.
            "total_source_rows": self.total_source_rows,
            "warning_threshold": self.config.get("warning_threshold", "Unknown"),
            "error_threshold": self.config.get("error_threshold", "Unknown"),
            "data_version": self._reported(self.data_version),
        }

        # Rendering and PDF generation are library work on data already in
        # hand. If they fail the deployment is broken, and that is caught in
        # test rather than worked around here.
        from chathealthy_lib.notification_client import NotificationClient
        from chathealthy_lib.discrepancy_pdf import build_discrepancy_pdf, render_header_as_html

        all_discrepancies = fatal_reports + error_reports + warning_reports
        body = render_header_as_html(manifest, all_discrepancies)
        attachments = [{
            "filename": "discrepancies.pdf",
            "content": build_discrepancy_pdf(manifest, all_discrepancies),
        }]

        receivers = self._resolve_recipients()
        status = self.manifest_status or ("failed" if is_fatal else "succeeded")
        detail = ("" if is_fatal
                  else ", no discrepancies" if warning_count == 0 and error_count == 0
                  else f", {warning_count} warning(s) {error_count} error(s)")
        subject = f"Provider pipeline {self.run_id} - {status}{detail}"
        log_context = (
            f"warnings={warning_count} errors={error_count} "
            f"warning_threshold={self.config.get('warning_threshold', 'Unknown')} "
            f"error_threshold={self.config.get('error_threshold', 'Unknown')} "
            f"mongo_down={self.mongo_down} "
            f"data_version={self._reported(self.data_version)}"
        )

        client = NotificationClient()
        delivered, last_failure = 0, None
        for address in receivers:
            try:
                client.send_email(
                    address, subject, body,
                    attachments=attachments, log_context=log_context,
                )
                delivered += 1
            except Exception as exc:  # noqa: BLE001 -- failure mode 1
                last_failure = exc
        if delivered:
            return True
        raise ChatHealthyException(
            mode="fatal_report_undeliverable",
            message=(
                f"the fatal report for run {self.run_id} reached none of its "
                f"{len(receivers)} recipients: "
                f"{type(last_failure).__name__}: {str(last_failure)[:200]}"
            ),
            component="DiscrepancyReport",
            run_id=self.run_id,
        ) from last_failure

    def _resolve_recipients(self) -> list[str]:
        """Every address the report goes to, or failure mode 2.

        The design calls for a receiver list, so each source contributes and
        the operator and the vault address both receive. Giving up on all of
        them is a raise -- a report with nowhere to go is not a report.
        """
        candidates = [
            self.operator_email,
            os.environ.get("NOTIFICATION_TO_EMAIL", ""),
        ]
        try:
            candidates.append(self._get_operator_email_from_vault())
        except Exception:  # noqa: BLE001 -- the vault is not a failure mode
            pass
        receivers: list[str] = []
        for candidate in candidates:
            address = (candidate or "").strip()
            if (address and "@" in address
                    and address.endswith(VALID_EMAIL_SUFFIXES)
                    and address not in receivers):
                receivers.append(address)
        if receivers:
            return receivers
        raise ChatHealthyException(
            mode="fatal_report_no_recipient",
            message=(
                f"the fatal report for run {self.run_id} has no valid address to "
                f"send to; tried the operator email, NOTIFICATION_TO_EMAIL and "
                f"the vault"
            ),
            component="DiscrepancyReport",
            run_id=self.run_id,
        )

    def createWarning(
        self,
        job_name: str,
        source: str,
        details: str,
        data_source: str,
        record_id: str,
        source_line: str | None = None,
        npi: str | None = None,
        field_list: list[str] | None = None,
    ) -> bool:
        """Record a warning using the report's mongo connection.

        Args:
            job_name: Name of the job/pipeline.
            source: Source location of the warning.
            details: What went wrong, in operator-readable prose.
            data_source: Data source (e.g., "NPPES", "NUCC").
            record_id: Business key of the record (e.g., NPI, NUCC code).
            source_line: Line in the source file the record came from.
            npi: The record's NPI. Defaults to record_id.
            field_list: Every field implicated in the warning.

        Returns:
            True if recorded successfully, False otherwise.
        """
        log = ChatHealthyLoggingService()
        try:
            if not self.mongo_connection:
                return False

            reports_coll = self.mongo_connection[PIPELINE_ADMIN_DB][DISCREPANCIES_COLLECTION]
            warning_doc = {
                "run_id": self.run_id,
                "job_name": job_name,
                "source": source,
                "data_source": data_source,
                "record_id": record_id,
                "level": "warning",
                "details": details,
                "explanation": details,
                "npi": npi or record_id,
                "source_line": source_line,
                "field_list": list(field_list or []),
            }
            reports_coll.insert_one(warning_doc)
            log.warning("data warning [%s]: %s", source, details)
            self.increment_warning_count()
            if check_threshold_and_trigger_fatal_if_needed(self, "warning"):
                return False
            return True
        except ChatHealthyException as exc:
            log.error("could not record warning: %s", exc, exc=exc)
            return False
        except Exception as exc:
            log.error("could not record warning: %s", exc)
            return False

    def createNonFatalError(
        self,
        job_name: str,
        source: str,
        details: str,
        data_source: str,
        record_id: str,
        source_line: str | None = None,
        npi: str | None = None,
        field_list: list[str] | None = None,
    ) -> bool:
        """Record a non-fatal error using the report's mongo connection.

        Args:
            job_name: Name of the job/pipeline.
            source: Source location of the error.
            details: What went wrong, in operator-readable prose.
            data_source: Data source (e.g., "NPPES", "NUCC").
            record_id: Business key of the record (e.g., NPI, NUCC code).
            source_line: Line in the source file the record came from.
            npi: The record's NPI. Defaults to record_id.
            field_list: Every field implicated in the error.

        Returns:
            True if recorded successfully, False otherwise.
        """
        log = ChatHealthyLoggingService()
        try:
            if not self.mongo_connection:
                return False

            reports_coll = self.mongo_connection[PIPELINE_ADMIN_DB][DISCREPANCIES_COLLECTION]
            error_doc = {
                "run_id": self.run_id,
                "job_name": job_name,
                "source": source,
                "data_source": data_source,
                "record_id": record_id,
                "level": "error",
                "details": details,
                "explanation": details,
                "npi": npi or record_id,
                "source_line": source_line,
                "field_list": list(field_list or []),
            }
            reports_coll.insert_one(error_doc)
            log.error("data error [%s]: %s", source, details)
            self.increment_error_count()
            if check_threshold_and_trigger_fatal_if_needed(self, "error"):
                return False
            return True
        except ChatHealthyException as exc:
            log.error("could not record non-fatal error: %s", exc, exc=exc)
            return False
        except Exception as exc:
            log.error("could not record non-fatal error: %s", exc)
            return False


def fatal_error(
    report: DiscrepancyReport,
    level: str | DiscrepancyDetail,
    explanation: str,
    source_line: str | None = None,
    records_processed: int = 0,
    rows_before_fatal: int = 0,
) -> bool:
    """Handle fatal error: write to mongo and send email report.

    Args:
        report: DiscrepancyReport with mongo connection.
        level: Fatal error severity level.
        explanation: Fatal error explanation.
        source_line: Fatal error source location.
        records_processed: Number of records processed before fatal error.
        rows_before_fatal: Accepted for caller compatibility and ignored.
            Row counts are supplied on the DiscrepancyReport constructor by
            the caller, which is the only party that can query them.

    Returns:
        True if report sent successfully, False otherwise.
    """
    log = ChatHealthyLoggingService()
    try:
        if not report.mongo_connection:
            return False

        if isinstance(level, str):
            level_enum = DiscrepancyDetail(level)
        else:
            level_enum = level

        # Query for actual warning/error counts from discrepancy_reports collection
        discs_coll_name = DISCREPANCIES_COLLECTION
        discs_coll = report.mongo_connection[PIPELINE_ADMIN_DB][discs_coll_name]

        warning_count = discs_coll.count_documents({"run_id": report.run_id, "level": "warning"})
        error_count = discs_coll.count_documents({"run_id": report.run_id, "level": "error"})

        # Build manifest for PDF generation
        end_time = datetime.now(timezone.utc).isoformat()
        manifest = {
            "run_id": report.run_id,
            "pipeline_name": report.pipeline_name,
            "run_started_utc": report.start_time,
            "run_ended_utc": end_time,
            "fatal_reason": explanation,
            "records_100_percent_successfully_collected": False,
            "records_with_non_fatal_warnings": warning_count,
            "records_with_non_fatal_errors": error_count,
            "rows_in_target": report._reported(report.rows_in_target),
            "target_collection": report._target_collection_name(),
            "total_rows": report._reported(report.total_rows),
            "total_source_rows": report.total_source_rows,
            "warning_threshold": report.config.get("warning_threshold", "Unknown"),
            "error_threshold": report.config.get("error_threshold", "Unknown"),
            "data_version": report._reported(report.data_version),
        }

        # fatal_error defaults to False on every log record and is only set
        # when a caller says so. This is the one place that should.
        _log.error(
            "FATAL %s: %s", report.pipeline_name, explanation,
            extra={"fatal_error": True},
        )

        # Write fatal error document
        reports_coll = report.mongo_connection[PIPELINE_ADMIN_DB][DISCREPANCIES_COLLECTION]
        fatal_doc = {
            "run_id": report.run_id,
            "job_name": report.pipeline_name,
            "source": source_line or report.source,
            "level": level_enum.value,
            "explanation": explanation,
            "manifest": manifest,
        }
        reports_coll.insert_one(fatal_doc)
        return report._write_email()
    except ChatHealthyException as exc:
        log.error("could not record the fatal error: %s", exc, exc=exc)
        return False
    except Exception as exc:
        log.error("could not record the fatal error: %s", exc)
        return False


def check_threshold_and_trigger_fatal_if_needed(
    report: DiscrepancyReport,
    level: str,
) -> bool:
    """Check if warning/error threshold reached; trigger fatal_error if so.

    Args:
        report: DiscrepancyReport with mongo connection.
        level: "warning" or "error"

    Returns:
        True if fatal was triggered, False otherwise.
    """
    log = ChatHealthyLoggingService()
    try:
        if not report.mongo_connection:
            return False

        # Get current counts
        counts = report.get_counts()

        if level == "warning":
            current_count = counts.get("warning_count", 0)
            threshold = report.config.get("warning_threshold")
        elif level == "error":
            current_count = counts.get("error_count", 0)
            threshold = report.config.get("error_threshold")
        else:
            return False

        # Check if threshold reached
        if threshold and current_count >= threshold:
            explanation = f"Non-fatal {level}s exceeded threshold: {current_count} >= {threshold}"
            return fatal_error(report, DiscrepancyDetail.FATAL, explanation)

        return False
    except ChatHealthyException as exc:
        log.error("threshold check failed: %s", exc, exc=exc)
        return False
    except Exception as exc:
        log.error("threshold check failed: %s", exc)
        return False


def createWarning(
    report: DiscrepancyReport,
    job_name: str,
    source: str,
    details: str,
    data_source: str,
    record_id: str,
) -> bool:
    """Record a warning using the report's mongo connection.

    Args:
        report: DiscrepancyReport with mongo connection.
        job_name: Name of the job/pipeline.
        source: Source location of the warning.
        details: Warning details.
        data_source: Data source (e.g., "NPPES", "NUCC").
        record_id: Business key of the record (e.g., NPI, NUCC code).

    Returns:
        True if recorded successfully, False otherwise.
    """
    log = ChatHealthyLoggingService()
    try:
        if not report.mongo_connection:
            return False

        reports_coll = report.mongo_connection[PIPELINE_ADMIN_DB][DISCREPANCIES_COLLECTION]
        warning_doc = {
            "run_id": report.run_id,
            "job_name": job_name,
            "source": source,
            "data_source": data_source,
            "record_id": record_id,
            "level": "warning",
            "details": details,
        }
        reports_coll.insert_one(warning_doc)
        log.warning("data warning [%s]: %s", source, details)
        report.increment_warning_count()
        if check_threshold_and_trigger_fatal_if_needed(report, "warning"):
            return False
        return True
    except ChatHealthyException as exc:
        log.error("could not record warning: %s", exc, exc=exc)
        return False
    except Exception as exc:
        log.error("could not record warning: %s", exc)
        return False


def CreateNonFatalError(
    report: DiscrepancyReport,
    job_name: str,
    source: str,
    details: str,
    data_source: str,
    record_id: str,
) -> bool:
    """Record a non-fatal error using the report's mongo connection.

    Args:
        report: DiscrepancyReport with mongo connection.
        job_name: Name of the job/pipeline.
        source: Source location of the error.
        details: Error details.
        data_source: Data source (e.g., "NPPES", "NUCC").
        record_id: Business key of the record (e.g., NPI, NUCC code).

    Returns:
        True if recorded successfully, False otherwise.
    """
    log = ChatHealthyLoggingService()
    try:
        if not report.mongo_connection:
            return False

        reports_coll = report.mongo_connection[PIPELINE_ADMIN_DB][DISCREPANCIES_COLLECTION]
        error_doc = {
            "run_id": report.run_id,
            "job_name": job_name,
            "source": source,
            "data_source": data_source,
            "record_id": record_id,
            "level": "error",
            "details": details,
        }
        reports_coll.insert_one(error_doc)
        log.error("data error [%s]: %s", source, details)
        report.increment_error_count()
        if check_threshold_and_trigger_fatal_if_needed(report, "error"):
            return False
        return True
    except ChatHealthyException as exc:
        log.error("could not record non-fatal error: %s", exc, exc=exc)
        return False
    except Exception as exc:
        log.error("could not record non-fatal error: %s", exc)
        return False


def emit_discrepancy_report(
    pipeline_mongo,
    run_id: str,
    manifest_status: str,
    manifest_doc: dict,
    config: dict,
    target_collection: str = "",
    total_source_rows: int | None = None,
    rows_in_target: int | None = None,
    total_rows: int | None = None,
    operator_email: str | None = None,
    operator_sms: str | None = None,
) -> dict:
    """Emit discrepancy report for the completed run.

    Reads all discrepancies from MongoDB, sends email to operator, and returns
    summary. Operator email defaults to NOTIFICATION_TO_EMAIL from environment
    if not provided. This is called at job completion by control_runner.

    Args:
        pipeline_mongo: MongoDB connection to the metadata database
        run_id: Run identifier
        manifest_status: Final status of the run (succeeded, failed, etc.)
        manifest_doc: Run manifest document
        config: Pipeline configuration
        target_collection: "<db>.<collection>" this pipeline publishes to,
            printed in the header and never queried
        total_source_rows / rows_in_target / total_rows: the three counts,
            taken by the caller. None where the caller does not know, which
            the report states as Unknown rather than zero.
        operator_email: Optional operator email; falls back to env var
        operator_sms: Optional operator SMS number (reserved for future use)

    Returns:
        dict with "total" (discrepancy count) and "pdf_bytes" (report size)
    """
    log = ChatHealthyLoggingService()

    try:
        # Create report instance with operator email
        report = DiscrepancyReport(
            run_id=run_id,
            env=os.environ.get("ENV_PREFIX", "dev"),
            pipeline_name=os.environ.get("PIPELINE_NAME", "provider"),
            source="ProviderPipelineOnDemand",
            data_version=(int(os.environ["DATA_VERSION"])
                          if os.environ.get("DATA_VERSION", "").strip().isdigit()
                          else None),
            target_collection=target_collection,
            total_source_rows=total_source_rows,
            rows_in_target=rows_in_target,
            total_rows=total_rows,
        )

        # Override with passed-in operator email, or use environment default
        if operator_email:
            report.operator_email = operator_email
        elif not report.operator_email:
            report.operator_email = os.environ.get("NOTIFICATION_TO_EMAIL", "").strip()

        # A caller may hand in its own connection; otherwise the report uses
        # the one it opened for itself.
        if pipeline_mongo is not None:
            report.mongo_connection = pipeline_mongo
            report.mongo_down = False

        # Both of these arrive as arguments and were both ignored, so the
        # report could not say what the run did or how much it moved.
        report.manifest_status = (manifest_status or "").strip().lower()
        # Fill gaps only. Assigning the caller's config over the top replaced
        # the one the report loads at construction, which is where the
        # warning and error thresholds live -- so both became Unknown.
        for key, value in (config or {}).items():
            report.config.setdefault(key, value)
        if isinstance(manifest_doc, dict) and manifest_doc.get("fatal_exception"):
            report.fatal_error = True

        total = 0
        if not report.mongo_down and report.mongo_connection is not None:
            try:
                total = report.mongo_connection[PIPELINE_ADMIN_DB][
                    DISCREPANCIES_COLLECTION].count_documents({"run_id": run_id})
            except Exception as exc:  # noqa: BLE001
                report.mongo_down = True
                report.mongo_down_reason = f"{type(exc).__name__}: {str(exc)[:200]}"

        # Send email if operator email is set
        email_sent = False
        if report.operator_email and "@" in report.operator_email:
            email_sent = report._write_email()
            if email_sent:
                log.info(
                    "emit_discrepancy_report: email sent to %s run_id=%s",
                    report.operator_email, run_id,
                )
            else:
                log.warning(
                    "emit_discrepancy_report: email send failed run_id=%s to=%s",
                    run_id, report.operator_email,
                )
        else:
            log.warning(
                "emit_discrepancy_report: operator_email not configured run_id=%s",
                run_id,
            )

        return {
            "total": total,
            "pdf_bytes": 0,  # PDF generation deferred
            "email_sent": email_sent,
            "operator_email": report.operator_email,
        }

    except ChatHealthyException as exc:
        log.error("could not emit discrepancy report: %s", exc, exc=exc)
        return {"total": 0, "pdf_bytes": 0, "error": str(exc)}
    except Exception as exc:
        log.error("unexpected failure emitting discrepancy report: %s", exc)
        return {"total": 0, "pdf_bytes": 0, "error": str(exc)}


def run_step(ctx) -> dict:
    """Step 23 -- report what this run found.

    The registry resolves a step by looking for run_step or execute on the
    module.
    """
    manifest = ctx.manifest
    run_id = manifest.run_id
    manifest_doc = {
        "run_id": run_id,
        "pipeline_name": manifest.pipeline_name,
        "status": manifest.status,
        "state_scope": getattr(ctx.args, "state_scope", None) or getattr(ctx.args, "states", None),
        "data_version": getattr(ctx.args, "data_version", None),
        "completed_steps": sorted(manifest.completed_steps),
    }
    summary = emit_discrepancy_report(
        None,
        run_id=run_id,
        manifest_status=manifest.status,
        manifest_doc=manifest_doc,
        config=ctx.config,
    )
    _log.info("discrepancy report: run_id=%s total=%s pdf_bytes=%s",
              run_id, summary.get("total"), summary.get("pdf_bytes"))
    return summary


def execute(ctx) -> dict:
    return run_step(ctx)
