# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""BasePipelineOrchestrator — shared scaffolding for every pipeline orchestrator.

Realizes EPIC-010-F-102-S-007 B3: each pipeline orchestration is 100% independent
of the others except that they share this single base class. The base owns the
reservation + IDLE-wait + try/release scaffolding; subclasses own only their
pipeline-specific steps.

Subclasses MUST set `requester_name` and implement `_pipeline_steps()` (a
generator). The generator may yield context activities / sub-orchestrators and
may return a result that becomes the orchestrator's return value.
"""

from __future__ import annotations

import datetime
import logging


class BasePipelineOrchestrator:
    """Shared scaffolding for pipeline orchestrators. Subclass per pipeline."""

    requester_name: str = ""

    def __init__(self, context, config: dict) -> None:
        self.context = context
        self.config = config

    # ────────────────────────────────────────────────────────────────────────
    def run(self):
        """Generator: register reservation, wait for IDLE, run pipeline steps,
        release reservation on success or failure."""
        if not self.requester_name:
            raise ValueError(
                f"{type(self).__name__}.requester_name must be set on the subclass"
            )

        cluster_name = self.config["pipeline_cluster"]
        job_id = self.context.instance_id
        reservation = {
            "job_id": job_id,
            "requester": self.requester_name,
            "cluster_name": cluster_name,
            "expected_duration_minutes": self.config["expected_duration_minutes"],
        }

        self.context.set_custom_status("Step 1: Waking cluster")
        yield self.context.call_activity(
            "wake_cluster_activity",
            {"cluster_name": cluster_name, "job_id": job_id},
        )

        deadline = self.context.current_utc_datetime + datetime.timedelta(minutes=15)
        while self.context.current_utc_datetime < deadline:
            status = yield self.context.call_activity(
                "check_cluster_state_activity",
                {"cluster_name": cluster_name, "job_id": job_id},
            )
            if status.get("cluster_state") == "IDLE":
                break
            next_check = self.context.current_utc_datetime + datetime.timedelta(seconds=30)
            yield self.context.create_timer(next_check)

        self.context.set_custom_status("Step 2: Reserving cluster")
        yield self.context.call_activity("register_reservation_activity", reservation)

        try:
            result = yield from self._pipeline_steps()
        finally:
            self.context.set_custom_status("Releasing cluster reservation")
            yield self.context.call_activity("release_reservation_activity", {"job_id": job_id})
        return result

    # ────────────────────────────────────────────────────────────────────────
    def _pipeline_steps(self):
        """Subclass-implemented generator carrying the pipeline-specific work.

        MUST be a generator (use yield). MAY return a dict that becomes the
        orchestrator's final result.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement _pipeline_steps()"
        )
