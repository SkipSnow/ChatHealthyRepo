# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# EvidencePackage — the only handoff between Ops Manager and Dev Manager.
# Structured JSON. Validated before sending.

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

MAX_RECENT_EVENTS = 10


@dataclass
class EvidencePackage:
    """The only communication between Ops Manager and Dev Manager.

    Ops Manager creates this. Dev Manager receives it and nothing else.
    """
    error_code: str
    error_message: str
    classification: str           # INFRASTRUCTURE | PIPELINE | UNKNOWN
    confidence: float             # 0.0 - 1.0
    reasoning: str                # why this classification
    cluster_state: str            # current cluster state
    recent_events: list[dict] = field(default_factory=list)  # max 10
    job_id: str = ""
    requester: str = ""
    first_seen_at: str = ""
    occurrence_count: int = 1
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        # Cap recent_events at MAX_RECENT_EVENTS
        if len(self.recent_events) > MAX_RECENT_EVENTS:
            self.recent_events = self.recent_events[-MAX_RECENT_EVENTS:]

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> list[str]:
        """Return list of validation errors. Empty = valid."""
        errors = []
        if not self.error_code:
            errors.append("error_code is required")
        if not self.error_message:
            errors.append("error_message is required")
        if self.classification not in ("INFRASTRUCTURE", "PIPELINE", "UNKNOWN"):
            errors.append(f"classification must be INFRASTRUCTURE|PIPELINE|UNKNOWN, got: {self.classification}")
        if not (0.0 <= self.confidence <= 1.0):
            errors.append(f"confidence must be 0.0-1.0, got: {self.confidence}")
        if len(self.recent_events) > MAX_RECENT_EVENTS:
            errors.append(f"recent_events exceeds max {MAX_RECENT_EVENTS}")
        return errors

    @staticmethod
    def from_dict(d: dict) -> "EvidencePackage":
        valid_fields = {f.name for f in EvidencePackage.__dataclass_fields__.values()}
        return EvidencePackage(**{k: v for k, v in d.items() if k in valid_fields})
