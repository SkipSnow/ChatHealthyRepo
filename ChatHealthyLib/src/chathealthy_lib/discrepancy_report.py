# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Discrepancy reporting for pipeline operations.

Workers write every per-record finding into pipelineAdmin.discrepancyLog as
two document kinds (LLD v54 §7.5): a per-record `record` document and a
per-finding-class `type_aggregate` document. The delivered report is produced
from the `type_aggregate` documents alone -- it groups by finding class,
prints each class's severity and count, and samples up to
report_cap_per_class keys per class into a trailing Appendix. Fatal errors are
emitted via DiscrepancyReport.fatal().
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

# One unified store holds both document kinds the workers write and the report
# reads (LLD v54 §7.5): per-record `record` documents and per-finding-class
# `type_aggregate` documents. The report is produced from the aggregates alone.
DISCREPANCY_LOG_COLLECTION = "discrepancyLog"
VALID_EMAIL_SUFFIXES = (".com", ".ai", ".org", ".net", ".edu", ".gov")
PIPELINE_CONFIG_COLLECTION = "PipelineConfig"

# Report display cap: at most this many sample keys per class in the appendix.
REPORT_CAP_DEFAULT = 25
# The reserved class a count that could not be taken is carried under (§7.5).
DISCREPANCY_REPORT_ERROR_CLASS = "discrepancy_report_error"
# Order the report lists severities in: the fatal first, then error, warning.
_SEVERITY_RANK = {"fatal": 0, "error": 1, "warning": 2}

_log = ChatHealthyLoggingService()


class FatalErrorReason(str, Enum):
    """Fatal error classifications."""
    MONGO_UNREACHABLE = "mongo_unreachable"
    VAULT_UNREACHABLE = "vault_unreachable"


class DiscrepancyDetail(str, Enum):
    """Discrepancy severity level.

    ERROR (row blocking):
      - Row cannot be loaded/parsed (parse failure, required field missing)

    WARNING (non-blocking):
      - Enrichment data inconsistent or missing but row is still usable
      - County not found after all enrichment step filter functions have run
        (county_unresolvable is graded warning in finding_types, LLD v54 §16)
      - Optional field populated with default
      - Data quality flag raised but row can proceed
      - Any issue that does not prevent the row from being published

    FATAL:
      - Job-terminating failure (MongoDB unreachable, vault unreachable, etc.)
      - A finding class graded fatal in finding_types (a pipeline defect)
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

        A count the client does not know is passed as None and reported as
        "Unknown". An absent count is never rendered as zero.
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
        # Bound FIRST, before any work that logs.
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
        self.mongo_down_reason = ""

        # Discrepancy log, and pipeline config are metadata: admin target,
        # reachable while the data factory is down. "Down" is a state this
        # object records and carries, never one it raises on.
        self.mongo_connection = None
        self.config = {}
        # Whether the durable config/metadata block was actually read. The one
        # sanctioned recipient fallback (REQ-B-006) is gated strictly on this
        # being False -- the config store being UNREACHABLE -- never on a merely
        # empty or misconfigured recipient list.
        self.config_loaded = False
        # The connection is opened for the discrepancyLog aggregates the report
        # reads. Durable CONFIG is NOT read here -- it is the brain
        # pipeline_config.json record the caller loads and passes to
        # emit_discrepancy_report, which sets self.config and config_loaded.
        try:
            self.mongo_connection = ChatHealthyMongoUtilities().getConnection(
                "pipelineEditor", "ChatHealthyFrontEnd")
        except Exception as exc:  # noqa: BLE001 -- see above
            self.mongo_down = True
            self.mongo_down_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            _log.warning("discrepancy report: metadata database unreachable (%s); "
                         "the report proceeds without it", self.mongo_down_reason)
        _log.info("discrepancy report opened: pipeline=%s source=%s mongo_down=%s",
                  self.pipeline_name, self.source, self.mongo_down)

    def __del__(self) -> None:
        """The class has no finally of its own, so teardown lands here."""
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
        """Fetch operator email from Key Vault (best-effort, non-recipient)."""
        try:
            return self._get_secret_value("notification-to-email")
        except ChatHealthyException:
            return None

    def _get_secret_value(self, secret_name: str) -> str | None:
        """Fetch a named Key Vault secret's value.

        Raises rather than swallows: a recipient list the config names but the
        vault will not yield is a loud failure, not a silent empty list.
        """
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            vault_uri = os.environ.get("KEY_VAULT_URI", "").strip()
            if not vault_uri:
                raise ChatHealthyException(
                    mode="discrepancy_report_vault_uri_missing",
                    component="DiscrepancyReport",
                    message=("KEY_VAULT_URI is not set, so the recipient secret "
                             f"{secret_name!r} cannot be read"),
                    run_id=self.run_id, secret_name=secret_name,
                )
            credential = DefaultAzureCredential()
            client = SecretClient(vault_uri=vault_uri, credential=credential)
            secret = client.get_secret(secret_name)
            return (secret.value or "").strip() if secret else None
        except ChatHealthyException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ChatHealthyException(
                mode="discrepancy_report_subscribers_secret_unreadable",
                component="DiscrepancyReport",
                message=(f"the recipient secret {secret_name!r} could not be "
                         f"read from Key Vault: {type(exc).__name__}: "
                         f"{str(exc)[:200]}"),
                run_id=self.run_id, secret_name=secret_name, exception=exc,
            ) from exc

    @staticmethod
    def _parse_recipients(value: str | None) -> list[str]:
        """Split a secret's value into candidate addresses.

        The value is a delimited list -- comma, semicolon or whitespace
        separated. No regular expression: plain replace-and-split.
        """
        if not value:
            return []
        normalized = value.replace(";", ",").replace("\n", ",").replace("\r", ",").replace("\t", ",")
        return [part.strip() for part in normalized.split(",") if part.strip()]

    @staticmethod
    def _valid_addresses(candidates: list[str]) -> list[str]:
        """The deliverable, de-duplicated addresses among the candidates."""
        receivers: list[str] = []
        for candidate in candidates:
            address = (candidate or "").strip()
            if (address and "@" in address
                    and address.endswith(VALID_EMAIL_SUFFIXES)
                    and address not in receivers):
                receivers.append(address)
        return receivers

    def fatal(self, exc: ChatHealthyException) -> bool:
        """Report a fatal error the caller has constructed but not raised.

        Nothing about this path needs the database: the exception already
        carries everything the report must say. Two ways to fail, both raise:
        the mail server cannot be reached, and there is no valid address.
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

    def _fatal_class(self) -> str:
        """The finding-class name a job-level fatal is carried under.

        One convention across every job-level writer (this class and
        pipeline_fatal_recorder): fatal_<mode>.
        """
        exc = self.fatal_exception
        mode = getattr(exc, "mode", "") if exc is not None else ""
        return f"fatal_{mode}" if mode else "fatal_unknown"

    def _held_fatal_already_recorded(self) -> bool:
        """True when the held fatal is a domain-class fatal already in the log.

        write_finding records a domain fatal aggregate and marks its exception;
        adding a job-level marker for the same event would give the run two
        fatals, so the job-level write is skipped for it.
        """
        exc = self.fatal_exception
        return bool(exc is not None
                    and getattr(exc, "context", {}).get("already_recorded_fatal"))

    def _record_fatal_in_mongo(self) -> None:
        """Persist the job-level fatal as a type_aggregate when the DB is there.

        A failure here flags the database down and returns. It is not the
        report's purpose to write to Mongo; it is the report's purpose to
        reach the operator, and the fatal is already held in memory.
        """
        if self.mongo_down or self.mongo_connection is None:
            return
        # The domain fatal is already in the log; do not add a second marker.
        if self._held_fatal_already_recorded():
            return
        try:
            cls = self._fatal_class()
            self.mongo_connection[PIPELINE_ADMIN_DB][
                DISCREPANCY_LOG_COLLECTION].update_one(
                {"_id": f"{self.run_id}:run:type:{cls}"},
                {
                    "$setOnInsert": {
                        "kind": "type_aggregate",
                        "run_id": self.run_id,
                        "artifact": "run",
                        "class": cls,
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "$set": {"severity": DiscrepancyDetail.FATAL.value,
                             "explanation": self._fatal_explanation()},
                    "$inc": {"count": 1},
                },
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.mongo_down = True
            self.mongo_down_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            _log.warning("discrepancy report: could not persist the fatal error (%s); "
                         "the report still goes out", self.mongo_down_reason)

    def _aggregates(self) -> list[dict] | None:
        """Every type_aggregate document for this run, or None if uncollectable.

        None is the "the count could not be taken" signal (§7.5): the report
        renders it as a discrepancy_report_error rather than as zero findings.
        """
        if self.mongo_down or self.mongo_connection is None:
            return None
        try:
            coll = self.mongo_connection[PIPELINE_ADMIN_DB][DISCREPANCY_LOG_COLLECTION]
            return list(coll.find({"run_id": self.run_id, "kind": "type_aggregate"}))
        except Exception as exc:  # noqa: BLE001
            self.mongo_down = True
            self.mongo_down_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            _log.warning("discrepancy report: could not read the discrepancy "
                         "aggregates (%s); the report says Unknown",
                         self.mongo_down_reason)
            return None

    def _report_cap(self) -> int:
        cap = ((self.config or {}).get("discrepancy_report") or {}).get(
            "report_cap_per_class")
        return cap if isinstance(cap, int) and cap >= 0 else REPORT_CAP_DEFAULT

    def _report_model(self, aggregates: list[dict] | None, cap: int) -> dict:
        """Group the aggregates into the report's body summary and appendix.

        Body = per-class {class, severity, count}. Appendix = per-class the
        first `cap` keys with an overflow count. Per-severity totals sum count.
        """
        uncollectable = aggregates is None
        nonfatal_summary: list[dict] = []
        nonfatal_appendix: list[dict] = []
        fatal_aggs: list[dict] = []
        warning_total = error_total = 0
        touched: set[str] = set()
        for agg in (aggregates or []):
            cls = agg.get("class", "?")
            severity = (agg.get("severity") or "").lower()
            count = agg.get("count", 0)
            keys = list(agg.get("keys") or [])
            touched.update(keys)
            if severity == "fatal":
                fatal_aggs.append(agg)
                continue
            nonfatal_summary.append({"class": cls, "severity": severity, "count": count})
            nonfatal_appendix.append({
                "class": cls, "severity": severity,
                "keys": keys[:cap], "total": count, "overflow": max(0, len(keys) - cap),
            })
            if severity == "warning":
                warning_total += count if isinstance(count, int) else 0
            elif severity == "error":
                error_total += count if isinstance(count, int) else 0

        # Exactly one fatal per run (§7.5). Collapse every fatal aggregate --
        # the domain-class fatal and any job-level marker for the same event --
        # into a single reported fatal, preferring the domain-class one. A
        # held fatal (DB down, nothing written) still heads the report.
        fatal_entry = None
        if fatal_aggs:
            domain = [a for a in fatal_aggs
                      if a.get("artifact") != "run"
                      and not str(a.get("class", "")).startswith("fatal_")]
            chosen = domain[0] if domain else fatal_aggs[0]
            keys = list(chosen.get("keys") or [])
            count = chosen.get("count", 0)
            fatal_entry = {
                "class": chosen.get("class", "?"), "severity": "fatal",
                "count": count, "keys": keys[:cap], "total": count,
                "overflow": max(0, len(keys) - cap),
            }
        elif self.fatal_error or self.fatal_exception:
            fatal_entry = {
                "class": self._fatal_class(), "severity": "fatal",
                "count": 1, "keys": [], "total": 1, "overflow": 0,
            }
        fatal_total = 1 if fatal_entry else 0

        summary: list[dict] = []
        appendix: list[dict] = []
        if fatal_entry:
            summary.append({"class": fatal_entry["class"], "severity": "fatal",
                            "count": fatal_entry["count"]})
            appendix.append({
                "class": fatal_entry["class"], "severity": "fatal",
                "keys": fatal_entry["keys"], "total": fatal_entry["total"],
                "overflow": fatal_entry["overflow"],
            })
        summary.extend(nonfatal_summary)
        appendix.extend(nonfatal_appendix)

        # A count that could not be taken is stated, never rendered as zero.
        if uncollectable:
            summary.append({"class": DISCREPANCY_REPORT_ERROR_CLASS,
                            "severity": "error", "count": "Unknown"})

        summary.sort(key=lambda r: (_SEVERITY_RANK.get(r["severity"], 9), r["class"]))
        appendix.sort(key=lambda r: (_SEVERITY_RANK.get(r["severity"], 9), r["class"]))
        return {
            "summary": summary,
            "appendix": appendix,
            "warning_total": warning_total,
            "error_total": error_total,
            "fatal_total": fatal_total,
            "records_touched": len(touched),
            "uncollectable": uncollectable,
        }

    def _target_collection_name(self) -> str:
        """Short name of the collection this pipeline loads into."""
        if not self.target_collection:
            return "target collection"
        return str(self.target_collection).split(".")[-1]

    @staticmethod
    def _reported(count: int | None):
        """A count the caller supplied, or "Unknown" if it did not."""
        return "Unknown" if count is None else count

    def _write_email(self) -> bool:
        """Deliver the report on success and abnormal end alike.

        Read the aggregates, build the body summary and the appendix, and send.
        Two failure modes, both raised: the mail server cannot be reached, and
        there is no valid address. Everything else -- an unreachable database,
        absent counts -- changes what the mail says, never whether it is sent.
        """
        cap = self._report_cap()
        model = self._report_model(self._aggregates(), cap)
        warning_total = model["warning_total"]
        error_total = model["error_total"]

        is_fatal = bool(self.fatal_error or self.fatal_exception) or (
            self.manifest_status not in ("", "succeeded", "completed")) or (
            model["fatal_total"] > 0)
        explanation = self._fatal_explanation() if is_fatal else ""

        warning_display = "Unknown" if model["uncollectable"] else warning_total
        error_display = "Unknown" if model["uncollectable"] else error_total

        end_time = datetime.now(timezone.utc).isoformat()
        manifest = {
            "run_id": self.run_id,
            "pipeline_name": self.pipeline_name,
            "run_status": self.manifest_status or ("failed" if is_fatal else "succeeded"),
            "run_started_utc": self.start_time,
            "run_ended_utc": end_time,
            "fatal_reason": explanation,
            "records_100_percent_successfully_collected": (
                not is_fatal and not model["uncollectable"]
                and warning_total == 0 and error_total == 0),
            "records_with_non_fatal_warnings": warning_display,
            "records_with_non_fatal_errors": error_display,
            "records_touched": model["records_touched"],
            "rows_in_target": self._reported(self.rows_in_target),
            "target_collection": self._target_collection_name(),
            "total_rows": self._reported(self.total_rows),
            "total_source_rows": self.total_source_rows,
            "report_cap_per_class": cap,
            "data_version": self._reported(self.data_version),
        }

        # Rendering and PDF generation are library work on data already in
        # hand. If they fail the deployment is broken, caught in test.
        from chathealthy_lib.notification_client import NotificationClient
        from chathealthy_lib.discrepancy_pdf import build_discrepancy_pdf, render_header_as_html

        body = render_header_as_html(manifest, model["summary"])
        attachments = [{
            "filename": "discrepancy_report.pdf",
            "content": build_discrepancy_pdf(
                manifest, model["summary"], model["appendix"]),
        }]

        receivers = self._resolve_recipients()
        status = self.manifest_status or ("failed" if is_fatal else "succeeded")
        if is_fatal:
            detail = ""
        elif model["uncollectable"]:
            detail = ", discrepancy counts Unknown"
        elif warning_total == 0 and error_total == 0:
            detail = ", no discrepancies"
        else:
            detail = f", {warning_total} warning(s) {error_total} error(s)"
        subject = f"Provider pipeline {self.run_id} - {status}{detail}"
        log_context = (
            f"warnings={warning_display} errors={error_display} "
            f"fatal={model['fatal_total']} mongo_down={self.mongo_down} "
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
                f"the report for run {self.run_id} reached none of its "
                f"{len(receivers)} recipients: "
                f"{type(last_failure).__name__}: {str(last_failure)[:200]}"
            ),
            component="DiscrepancyReport",
            run_id=self.run_id,
        ) from last_failure

    def _resolve_recipients(self) -> list[str]:
        """Every address the report goes to.

        Recipients are neither hardcoded nor literal in config: the metadata
        block names a Key Vault secret (subscribers_secret) whose value is the
        recipient list (§5.2). The normal path reads that secret and fails
        loudly if the secret name is absent or the resolved list is empty.

        The one sanctioned escape (REQ-B-006): only when the config/metadata
        store is UNREACHABLE -- so subscribers_secret cannot be read at all --
        does the report fall back to an env recipient so it still delivers.
        """
        if not self.config_loaded:
            env_addr = (os.environ.get("NOTIFICATION_TO_EMAIL", "") or "").strip()
            receivers = self._valid_addresses([env_addr])
            if receivers:
                _log.warning(
                    "discrepancy report: config/metadata store unreachable; "
                    "delivering to the NOTIFICATION_TO_EMAIL recipient so the "
                    "report is not lost (REQ-B-006) run_id=%s", self.run_id)
                return receivers
            raise ChatHealthyException(
                mode="fatal_report_no_recipient",
                message=(
                    f"the report for run {self.run_id} cannot reach the config "
                    f"store to read its recipient secret, and NOTIFICATION_TO_"
                    f"EMAIL is unset, so it has no address to fall back to"
                ),
                component="DiscrepancyReport",
                run_id=self.run_id,
            )

        secret_name = ((self.config or {}).get("metadata") or {}).get("subscribers_secret")
        if not secret_name:
            raise ChatHealthyException(
                mode="discrepancy_report_subscribers_secret_missing",
                component="DiscrepancyReport",
                message=(
                    f"the report for run {self.run_id} has no "
                    f"metadata.subscribers_secret configured; the operator must "
                    f"name the Key Vault secret that holds the recipient list"
                ),
                run_id=self.run_id,
            )
        receivers = self._valid_addresses(
            self._parse_recipients(self._get_secret_value(secret_name)))
        if receivers:
            return receivers
        raise ChatHealthyException(
            mode="discrepancy_report_no_recipients",
            message=(
                f"the report for run {self.run_id} resolved no valid recipient "
                f"from the Key Vault secret {secret_name!r}; the secret's value "
                f"is empty or holds no deliverable address"
            ),
            component="DiscrepancyReport",
            run_id=self.run_id,
        )


def fatal_error(
    report: DiscrepancyReport,
    level: str | DiscrepancyDetail,
    explanation: str,
    source_line: str | None = None,
    records_processed: int = 0,
    rows_before_fatal: int = 0,
) -> bool:
    """Record a job-level fatal and deliver the report.

    Args:
        report: DiscrepancyReport with mongo connection.
        level: Fatal error severity level (retained for call compatibility).
        explanation: Fatal error explanation.
        source_line / records_processed / rows_before_fatal: accepted for
            caller compatibility and not used; the row counts are supplied on
            the DiscrepancyReport constructor by the caller.

    Returns True if the report was delivered.
    """
    report.fatal_exception = ChatHealthyException(
        mode="job_fatal",
        component="DiscrepancyReport",
        message=explanation,
        run_id=report.run_id,
    )
    report.fatal_error = True
    set_fatal_error(True)
    _log.error("FATAL %s: %s", report.pipeline_name, explanation,
               extra={"fatal_error": True})
    report._record_fatal_in_mongo()
    return report._write_email()


def check_threshold_and_trigger_fatal_if_needed(
    report: DiscrepancyReport,
    finding_class: str,
) -> bool:
    """Per-type escalation check (§7.5): does this class now abort the run?

    Severity is authoritative in finding_types. A class graded fatal, or one
    whose running count in discrepancyLog has reached its fatal_at_count,
    escalates to the run's single fatal and delivers the report.
    """
    log = ChatHealthyLoggingService()
    try:
        finding_types = (report.config or {}).get("discrepancy_report", {}).get(
            "finding_types") or {}
        entry = finding_types.get(finding_class)
        if not entry:
            return False
        severity = (entry.get("severity") or "").lower()
        fatal_at_count = entry.get("fatal_at_count")
        if severity == "fatal":
            return fatal_error(
                report, DiscrepancyDetail.FATAL,
                f"finding class {finding_class!r} is graded fatal")
        if fatal_at_count is None or report.mongo_connection is None:
            return False
        aggregate = report.mongo_connection[PIPELINE_ADMIN_DB][
            DISCREPANCY_LOG_COLLECTION].find_one(
            {"run_id": report.run_id, "kind": "type_aggregate",
             "class": finding_class}, {"count": 1})
        count = (aggregate or {}).get("count", 0)
        if count >= fatal_at_count:
            return fatal_error(
                report, DiscrepancyDetail.FATAL,
                f"finding class {finding_class!r} reached its fatal_at_count "
                f"({count} >= {fatal_at_count})")
        return False
    except ChatHealthyException as exc:
        log.error("threshold check failed: %s", exc, exc=exc)
        return False
    except Exception as exc:
        log.error("threshold check failed: %s", exc)
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
    """Emit the one operational report for the completed run.

    Reads the run's type_aggregate documents from discrepancyLog, delivers the
    report to the configured subscribers, and returns a summary. Called at job
    completion by control_runner on success and abnormal end alike.

    Returns dict with "total" (finding count across aggregates) and email
    delivery status.
    """
    log = ChatHealthyLoggingService()

    try:
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

        if operator_email:
            report.operator_email = operator_email
        elif not report.operator_email:
            report.operator_email = os.environ.get("NOTIFICATION_TO_EMAIL", "").strip()

        # A caller may hand in its own connection; otherwise the report uses
        # the one it opened for itself.
        if pipeline_mongo is not None:
            report.mongo_connection = pipeline_mongo
            report.mongo_down = False

        report.manifest_status = (manifest_status or "").strip().lower()
        # Durable configuration is the brain pipeline_config.json record the
        # caller loaded and passes in -- not Mongo. The report reads its
        # discrepancy_report and metadata blocks from it; config_loaded gates
        # the store-unreachable recipient fallback (REQ-B-006).
        if config:
            report.config = dict(config)
            report.config_loaded = True
        if isinstance(manifest_doc, dict) and manifest_doc.get("fatal_exception"):
            report.fatal_error = True

        total = 0
        if not report.mongo_down and report.mongo_connection is not None:
            try:
                aggregates = report.mongo_connection[PIPELINE_ADMIN_DB][
                    DISCREPANCY_LOG_COLLECTION].find(
                    {"run_id": run_id, "kind": "type_aggregate"}, {"count": 1})
                total = sum(
                    a.get("count", 0) for a in aggregates
                    if isinstance(a.get("count", 0), int))
            except Exception as exc:  # noqa: BLE001
                report.mongo_down = True
                report.mongo_down_reason = f"{type(exc).__name__}: {str(exc)[:200]}"

        # The report is delivered on every run, success and abnormal end alike.
        email_sent = report._write_email()
        if email_sent:
            log.info("emit_discrepancy_report: report delivered run_id=%s", run_id)
        else:
            log.warning("emit_discrepancy_report: report not delivered run_id=%s", run_id)

        return {
            "total": total,
            "pdf_bytes": 0,
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
    """Step -- report what this run found.

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
