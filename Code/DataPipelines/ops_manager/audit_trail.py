# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Audit Trail — append-only logging for Ops Manager.
# Triage decisions, repair actions, escalations, notifications.
# No updates. No deletes. Immutable.

import logging
from datetime import datetime, timezone

_log = logging.getLogger("ops.audit_trail")


class AuditTrail:
    """Append-only audit log for pipeline operations.

    Writes to MongoDB: {env_prefix}_System.ops_audit
    Records are immutable — no updates, no deletes.
    """

    def __init__(self, get_db_fn, env_prefix: str):
        self._get_db = get_db_fn
        self._coll_name = f"{env_prefix}_System"

    def _collection(self):
        db = self._get_db()
        if db is None:
            return None
        return db[self._coll_name]["ops_audit"]

    def _append(self, record: dict):
        record["timestamp"] = datetime.now(timezone.utc).isoformat()
        record["immutable"] = True
        coll = self._collection()
        if coll is not None:
            coll.insert_one(record)
            _log.info("Audit: %s — %s", record.get("event_type", "unknown"), record.get("summary", ""))
        else:
            _log.warning("Audit trail unavailable (no DB) — logging only: %s", record)

    def log_triage(self, error_code: str, classification: str, confidence: float,
                   source: str, reasoning: str, job_id: str = "", requester: str = ""):
        """Log a triage decision (rule-based or LLM)."""
        self._append({
            "event_type": "triage",
            "error_code": error_code,
            "classification": classification,
            "confidence": confidence,
            "source": source,
            "reasoning": reasoning,
            "job_id": job_id,
            "requester": requester,
            "summary": f"{source}: {error_code} -> {classification} ({confidence:.0%})",
        })

    def log_repair(self, action: str, attempt_number: int, outcome: str,
                   cluster_state_before: str = "", cluster_state_after: str = "",
                   job_id: str = ""):
        """Log a repair attempt."""
        self._append({
            "event_type": "repair",
            "action": action,
            "attempt_number": attempt_number,
            "outcome": outcome,  # success | failure | unchanged
            "cluster_state_before": cluster_state_before,
            "cluster_state_after": cluster_state_after,
            "job_id": job_id,
            "summary": f"repair #{attempt_number}: {action} -> {outcome}",
        })

    def log_escalation(self, recipient: str, reason: str, evidence_package: dict = None):
        """Log an escalation event."""
        self._append({
            "event_type": "escalation",
            "recipient": recipient,
            "reason": reason,
            "evidence_package": evidence_package or {},
            "summary": f"escalate to {recipient}: {reason}",
        })

    def log_notification(self, recipient: str, event_type: str, title: str, sent: bool):
        """Log a notification event."""
        self._append({
            "event_type": "notification",
            "recipient": recipient,
            "notification_type": event_type,
            "title": title,
            "sent": sent,
            "summary": f"notify {recipient}: {title} (sent={sent})",
        })

# DEAD CODE (v4-031) -- unreferenced function 'log_warning', marked for deletion
#     def log_warning(self, warning_type: str, job_id: str, details: dict = None):
#         """Log a warning (e.g., suspiciously fast job)."""
#         self._append({
#             "event_type": "warning",
#             "warning_type": warning_type,
#             "job_id": job_id,
#             "details": details or {},
#             "summary": f"warning: {warning_type} for {job_id}",
#         })
# END DEAD CODE
