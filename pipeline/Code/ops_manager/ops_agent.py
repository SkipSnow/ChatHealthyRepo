# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# OpsManagerAgent — Pipeline Operations Manager.
# Manages infrastructure. Zero pipeline knowledge.
# The agent uses tools. Tools don't think. The agent thinks.

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "Shared"))
from agent_framework.base_agent import BaseAgent
from agent_framework.base_tool import ToolResult

from .cluster_tool import ClusterTool
from .alert_tool import AlertTool
from .triage_tool import TriageTool
from .evidence_package import EvidencePackage
from .audit_trail import AuditTrail

_log = logging.getLogger("ops.agent")

# Config defaults — will move to Brain config
DEFAULT_MAX_DIAGNOSE_ITERATIONS = 3
DEFAULT_MAX_REPAIR_ATTEMPTS = 3

# Grace beyond a reservation's expected_end_time before the timer reaps it.
# Only applies to reservation_class == "automated"; human-class reservations
# are never auto-reaped. (BUG-006 fix R4 + F-029-S-001 REQ-T-008.)
GRACE_MINUTES = 30


def _is_overdue(reservation: dict, grace_minutes: int = GRACE_MINUTES) -> bool:
    """True if `now` is past expected_end_time + grace_minutes."""
    expected_end = reservation.get("expected_end_time", "")
    if not expected_end:
        return False
    try:
        end_dt = datetime.fromisoformat(expected_end)
    except (ValueError, TypeError):
        return False
    grace = timedelta(minutes=grace_minutes)
    return datetime.now(timezone.utc) > end_dt + grace


class OpsManagerAgent(BaseAgent):
    """Pipeline Operations Manager — infrastructure lifecycle agent.

    Owns: cluster state, reservations, cost, uptime.
    Never touches: pipeline data, task logic, collection schemas.
    """

    def __init__(self, lifecycle_manager, get_db_fn, env_prefix: str,
                 push_fn=None, email_fn=None):
        self._lifecycle_manager = lifecycle_manager
        self._audit = AuditTrail(get_db_fn, env_prefix)
        self._email_fn = email_fn
        self._push_fn = push_fn
        super().__init__(name="OpsManager", push_fn=push_fn)

    def register_tools(self) -> None:
        self.tools.register(ClusterTool(self._lifecycle_manager))
        self.tools.register(AlertTool(push_fn=self._push, email_fn=self._email_fn))
        self.tools.register(TriageTool())

    # ── Event handling ──────────────────────────────────────

    def handle_event(self, event: dict) -> ToolResult:
        """Main entry point. Receives events from timer, API, or error callback.

        Event types:
            timer_check     — 5-minute timer tick
            error           — pipeline error occurred
            wake            — wake cluster request
            release         — release reservation
            status          — check cluster status
        """
        event_type = event.get("type", "")

        if event_type == "timer_check":
            return self._handle_timer(event)
        elif event_type == "error":
            return self._handle_error(event)
        elif event_type in ("wake", "release", "status", "force_release"):
            return self.tools.execute("cluster", action=event_type, **event.get("params", {}))
        else:
            _log.warning("Unknown event type: %s", event_type)
            return ToolResult(success=False, error_code="UNKNOWN_EVENT",
                              error_message=f"Unknown event type: {event_type}")

    # ── Timer ───────────────────────────────────────────────

    def _handle_timer(self, event: dict) -> ToolResult:
        """5-minute timer. Infrastructure checks only. No task execution.

        Order matters:
          (1) Per-overdue alerts (fired every tick while overdue, regardless of class).
          (2) Reaper — DELETE rows that are overdue past GRACE_MINUTES AND class=='automated'.
              Reaping is the sole state-changing recovery action; deletion is via
              ClusterLifecycleManager.release(), which preserves the live-only contract
              and triggers the existing pause-on-last-release path.
          (3) Pause gate (class-aware) — pauses the cluster when no row is currently
              blocking. A row blocks if it is active AND not (overdue past grace AND
              class=='automated'). Human-class reservations always block until released.
        """
        cluster_name = event.get("cluster_name", "ChatHealthyDataPipelines")

        # Check cluster status
        status_result = self.tools.execute("cluster", action="status", cluster_name=cluster_name)
        if not status_result.success:
            return status_result

        data = status_result.data
        cluster_state = data.get("cluster_state", "UNKNOWN")
        reservations = data.get("reservations", [])

        # (1) Per-overdue alerts (informational; fired every tick while a row is past expected_end).
        self._check_overdue(reservations)

        # (2) Reaper. Delete overdue automated rows; emit a one-time reap alert per row.
        reaped_ids: list[str] = []
        for r in reservations:
            if r.get("reservation_class", "automated") != "automated":
                continue
            if not _is_overdue(r):
                continue
            job_id = r.get("job_id", "")
            if not job_id:
                continue
            release_result = self.tools.execute("cluster", action="release", job_id=job_id)
            if release_result.success:
                reaped_ids.append(job_id)
                self.tools.execute(
                    "alert", event_type="job_reaped", subject_id=job_id,
                    title="Reservation Reaped",
                    message=(
                        f"Job {job_id} ({r.get('requester','')}) reaped after "
                        f"{GRACE_MINUTES}+ min past expected end. Row deleted; cluster "
                        f"will pause if it was the last reservation."
                    ),
                )
                self._audit.log_notification(
                    "human", "job_reaped",
                    f"Job {job_id} reaped (overdue past {GRACE_MINUTES} min grace)", True,
                )
            else:
                _log.warning("Reaper: release(%s) failed: %s",
                             job_id, release_result.error_message)

        # If we reaped anything, the live reservations list changed underneath us.
        if reaped_ids:
            reaped_set = set(reaped_ids)
            reservations = [r for r in reservations if r.get("job_id") not in reaped_set]

        # (3) Class-aware pause gate. A row is "blocking" if it is active AND
        # we should NOT auto-pause around it: human rows always block; automated
        # rows block only while they are not overdue past grace.
        live_blocking = [
            r for r in reservations
            if r.get("status", "active") == "active"
            and not (
                _is_overdue(r)
                and r.get("reservation_class", "automated") == "automated"
            )
        ]
        if cluster_state == "IDLE" and not live_blocking:
            _log.info(
                "Cluster %s is IDLE with no blocking reservations (%d total live rows, "
                "%d reaped this tick) — pausing",
                cluster_name, len(reservations), len(reaped_ids),
            )
            self.tools.execute("cluster", action="force_release", cluster_name=cluster_name)

        # (4) Stuck cluster (running, but nothing is blocking pause).
        if cluster_state not in ("IDLE", "PAUSED") and not live_blocking:
            self.tools.execute(
                "alert", event_type="cluster_stuck", subject_id=cluster_name,
                title="Cluster Stuck",
                message=f"{cluster_name} is {cluster_state} with no blocking reservations",
            )

        return ToolResult(success=True, data={
            "timer_check": "complete",
            "cluster_state": cluster_state,
            "live_rows": len(reservations),
            "blocking_rows": len(live_blocking),
            "reaped_this_tick": len(reaped_ids),
        })

    def _check_overdue(self, reservations: list):
        """Check for overdue jobs. Notify human with cooldown."""
        now = datetime.now(timezone.utc)
        for r in reservations:
            expected_end = r.get("expected_end_time", "")
            if not expected_end:
                continue
            try:
                end_dt = datetime.fromisoformat(expected_end)
                if now > end_dt:
                    minutes_over = int((now - end_dt).total_seconds() / 60)
                    job_id = r.get("job_id", "unknown")
                    self.tools.execute(
                        "alert", event_type="job_overdue", subject_id=job_id,
                        title="Pipeline Overdue",
                        message=f"Job {job_id} ({r.get('requester', '')}) is {minutes_over} min "
                                f"past expected. Cluster {r.get('cluster_name', '')} still running.",
                    )
                    self._audit.log_notification("human", "job_overdue",
                                                  f"Job {job_id} overdue by {minutes_over} min", True)
            except (ValueError, TypeError):
                pass

    # ── Error handling ──────────────────────────────────────

    def _handle_error(self, event: dict) -> ToolResult:
        """Main error flow: notify -> diagnose -> route -> repair or escalate."""
        error_code = event.get("error_code", "UNKNOWN")
        error_message = event.get("error_message", "")
        cluster_name = event.get("cluster_name", "ChatHealthyDataPipelines")
        job_id = event.get("job_id", "")
        requester = event.get("requester", "")

        # Step 1: Notify human — error detected
        self.tools.execute("alert", event_type="error_detected",
                           title="Pipeline Error Detected",
                           message=f"Error {error_code} in job {job_id} ({requester}). Investigating.")
        self._audit.log_notification("human", "error_detected",
                                      f"Error {error_code} in {job_id}", True)

        # Step 2: Get cluster state for context
        status_result = self.tools.execute("cluster", action="status", cluster_name=cluster_name)
        cluster_state = "UNKNOWN"
        if status_result.success:
            cluster_state = status_result.data.get("cluster_state", "UNKNOWN")

        # Step 3: Triage — classify the error
        triage_result = self.tools.execute(
            "triage",
            error_code=error_code,
            error_message=error_message,
            cluster_state=cluster_state,
            job_id=job_id,
            requester=requester,
            recent_events=event.get("recent_events", []),
        )

        if not triage_result.success:
            self._escalate_unknown(error_code, error_message, cluster_state, job_id, requester)
            return triage_result

        classification = triage_result.data
        category = classification.get("category", "UNKNOWN")

        # Log triage decision
        self._audit.log_triage(
            error_code=error_code,
            classification=category,
            confidence=classification.get("confidence", 0.0),
            source=classification.get("source", "unknown"),
            reasoning=classification.get("reasoning", ""),
            job_id=job_id,
            requester=requester,
        )

        # Step 4: Route based on classification
        if category == "INFRASTRUCTURE":
            return self._handle_infra_error(error_code, error_message, cluster_name,
                                            cluster_state, classification, job_id, requester)
        elif category == "PIPELINE":
            return self._handle_pipeline_error(error_code, error_message, cluster_name,
                                               cluster_state, classification, job_id, requester)
        else:
            return self._escalate_unknown(error_code, error_message, cluster_state, job_id, requester)

    # ── Infrastructure repair loop ──────────────────────────

    def _handle_infra_error(self, error_code, error_message, cluster_name,
                            cluster_state, classification, job_id, requester) -> ToolResult:
        """Infra repair loop: attempt repair up to M times, verify each attempt."""
        max_attempts = DEFAULT_MAX_REPAIR_ATTEMPTS
        prev_state = cluster_state
        prev_error = error_code

        for attempt in range(1, max_attempts + 1):
            _log.info("Infra repair attempt %d/%d for %s", attempt, max_attempts, error_code)

            # Repair
            repair_result = self.attempt_self_repair(error_code, {"cluster_name": cluster_name})

            # Verify — read status after repair
            status_result = self.tools.execute("cluster", action="status", cluster_name=cluster_name)
            new_state = "UNKNOWN"
            if status_result.success:
                new_state = status_result.data.get("cluster_state", "UNKNOWN")

            # Log repair attempt
            outcome = "success" if new_state == "IDLE" else "failure"
            if new_state == prev_state and error_code == prev_error:
                outcome = "unchanged"

            self._audit.log_repair(
                action=classification.get("repair_action", "restart_cluster"),
                attempt_number=attempt,
                outcome=outcome,
                cluster_state_before=prev_state,
                cluster_state_after=new_state,
                job_id=job_id,
            )

            # Success?
            if new_state == "IDLE":
                self.tools.execute("alert", event_type="self_repaired",
                                   title="Infrastructure Self-Repaired",
                                   message=f"Error {error_code} in job {job_id} fixed. "
                                           f"Cluster {cluster_name} is IDLE.")
                self._audit.log_notification("human", "self_repaired",
                                              f"Infra repaired: {error_code}", True)
                return ToolResult(success=True, data={"repaired": True, "attempts": attempt})

            # Evidence-must-change guardrail
            if outcome == "unchanged":
                _log.warning("Evidence unchanged after repair — escalating early")
                break

            prev_state = new_state

        # Exhausted or unchanged — escalate
        evidence = EvidencePackage(
            error_code=error_code, error_message=error_message,
            classification="INFRASTRUCTURE", confidence=classification.get("confidence", 0.0),
            reasoning=f"Infra repair failed after {attempt} attempts",
            cluster_state=cluster_state, job_id=job_id, requester=requester,
        )
        self._audit.log_escalation("human", f"Infra repair failed after {attempt} attempts",
                                    evidence.to_dict())
        self.escalate_to_boss("Infrastructure Repair Failed",
                              f"Error {error_code} in job {job_id}. "
                              f"{attempt} repair attempts exhausted. Need help.")
        return ToolResult(success=False, error_code="REPAIR_EXHAUSTED",
                          error_message=f"Infra repair failed after {attempt} attempts")

    # ── Pipeline error handoff ──────────────────────────────

    def _handle_pipeline_error(self, error_code, error_message, cluster_name,
                               cluster_state, classification, job_id, requester) -> ToolResult:
        """Package evidence and hand off to Dev Manager."""
        evidence = EvidencePackage(
            error_code=error_code,
            error_message=error_message,
            classification="PIPELINE",
            confidence=classification.get("confidence", 0.0),
            reasoning=classification.get("reasoning", ""),
            cluster_state=cluster_state,
            job_id=job_id,
            requester=requester,
        )

        validation_errors = evidence.validate()
        if validation_errors:
            _log.error("EvidencePackage validation failed: %s", validation_errors)

        # Notify Dev Manager
        self.escalate_to_dev("Pipeline Code Error",
                             f"Error {error_code} in job {job_id} ({requester}). "
                             f"Classification: PIPELINE ({classification.get('confidence', 0):.0%}). "
                             f"Reasoning: {classification.get('reasoning', '')}")

        self._audit.log_escalation("dev", f"Pipeline error: {error_code}", evidence.to_dict())

        return ToolResult(success=True, data={
            "routed_to": "dev_manager",
            "evidence_package": evidence.to_dict(),
        })

    # ── Unknown / can't determine ───────────────────────────

    def _escalate_unknown(self, error_code, error_message, cluster_state,
                          job_id, requester) -> ToolResult:
        """Can't classify — escalate to human."""
        evidence = EvidencePackage(
            error_code=error_code, error_message=error_message,
            classification="UNKNOWN", confidence=0.0,
            reasoning="Could not classify error after triage",
            cluster_state=cluster_state, job_id=job_id, requester=requester,
        )
        self._audit.log_escalation("human", f"Unclassifiable error: {error_code}", evidence.to_dict())
        self.escalate_to_boss("Unclassifiable Error",
                              f"Error {error_code} in job {job_id}. Cannot determine if infra or code. Need help.")
        return ToolResult(success=False, error_code="UNKNOWN_CLASSIFICATION",
                          error_message="Could not classify error", data=evidence.to_dict())
