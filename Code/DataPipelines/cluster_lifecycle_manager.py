# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ClusterLifecycleManager — manages MongoDB Atlas cluster start/stop lifecycle.
# ResourceReservation — passed into pipelines to track expected duration.
#
# Design:
#   - Pipelines register a reservation before starting
#   - Pipeline finally{} releases reservation
#   - Manager checks reservations on a 1-hour timer
#   - Cluster stays up while reservations array is non-empty
#   - Last reservation released = cluster shuts down
#   - Overdue reservations trigger Pushover alert to Boss
#
# Deployed as Azure Function timer trigger (1-hour interval).

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

_log = logging.getLogger("pipeline.cluster_lifecycle")


@dataclass
class ResourceReservation:
    """A resource reservation passed into a pipeline at invocation.

    The pipeline holds this object. The finally block calls
    ClusterLifecycleManager.release(reservation).
    """
    job_id: str
    requester: str  # pipeline name or agent
    cluster_name: str  # which Atlas cluster
    expected_duration_minutes: int
    start_time: str = ""  # ISO 8601, set on register
    expected_end_time: str = ""  # ISO 8601, computed on register
    status: str = "pending"  # pending -> active -> released | expired

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ResourceReservation":
        return ResourceReservation(**{k: v for k, v in d.items() if k in ResourceReservation.__dataclass_fields__})


class ClusterLifecycleManager:
    """Manages MongoDB Atlas cluster lifecycle based on pipeline reservations.

    Holds an array of active reservations. Cluster is ON when array is non-empty.
    Cluster is OFF when array is empty. Alerts Boss on overdue jobs.

    State is stored in MongoDB admin collection (survives restarts).
    """

    def __init__(self, get_db_fn, env_prefix: str, push_fn=None):
        """
        Args:
            get_db_fn: callable returning MongoDB client
            env_prefix: dev/qa/prod
            push_fn: callable(title, message) for Pushover alerts to Boss
        """
        self._get_db = get_db_fn
        self._env = env_prefix
        self._push = push_fn
        self._collection_name = f"{env_prefix}_System"
        self._doc_id = "cluster_reservations"

    def _get_state(self) -> dict:
        """Read reservation state from MongoDB."""
        db = self._get_db()
        if db is None:
            return {"_id": self._doc_id, "reservations": []}
        doc = db[self._collection_name]["cluster_lifecycle"].find_one({"_id": self._doc_id})
        if not doc:
            doc = {"_id": self._doc_id, "reservations": []}
            db[self._collection_name]["cluster_lifecycle"].insert_one(doc)
        return doc

    def _save_state(self, state: dict):
        """Write reservation state to MongoDB."""
        db = self._get_db()
        if db is None:
            return
        db[self._collection_name]["cluster_lifecycle"].replace_one(
            {"_id": self._doc_id}, state, upsert=True
        )

    def register(self, reservation: ResourceReservation, task_config: dict = None) -> ResourceReservation:
        """Register a reservation. Wakes the cluster if needed. Non-blocking.

        If task_config is provided, the task is queued and the 5-minute timer
        will execute it when the cluster reaches IDLE. Returns immediately.
        """
        now = datetime.now(timezone.utc)
        reservation.start_time = now.isoformat()
        reservation.expected_end_time = (now + timedelta(minutes=reservation.expected_duration_minutes)).isoformat()
        reservation.status = "pending"  # pending until cluster is IDLE

        state = self._get_state()
        entry = reservation.to_dict()
        if task_config:
            entry["queued_task"] = task_config  # timer will execute this
        state["reservations"].append(entry)
        state["last_updated"] = now.isoformat()
        self._save_state(state)

        _log.info("Reservation registered: %s (expected %d min, queued: %s)",
                   reservation.job_id, reservation.expected_duration_minutes, bool(task_config))

        # Wake cluster — non-blocking
        self._wake_cluster(reservation.cluster_name)

        return reservation

    def release(self, reservation: ResourceReservation):
        """Release a reservation. Shuts down cluster if last one.

        Called in the pipeline's finally block. Always called — success or failure.
        """
        state = self._get_state()
        state["reservations"] = [
            r for r in state["reservations"]
            if r.get("job_id") != reservation.job_id
        ]
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._save_state(state)

        _log.info("Reservation released: %s", reservation.job_id)

        # Shut down if no more reservations
        if not state["reservations"]:
            self._shutdown_cluster(reservation.cluster_name)

    def process_queue(self):
        """Called by the 5-minute timer. Check cluster state, run queued tasks.

        If cluster is IDLE and there are pending reservations with queued tasks,
        execute them. Marks reservation as active once task starts.
        """
        state = self._get_state()
        pending = [r for r in state["reservations"] if r.get("status") == "pending" and r.get("queued_task")]
        if not pending:
            return

        # Check if cluster is up
        cluster_name = pending[0].get("cluster_name", "")
        if not cluster_name:
            return

        try:
            cluster_state = self._get_cluster_state(cluster_name)
        except Exception as e:
            _log.warning("Cannot check cluster state: %s", e)
            return

        if cluster_state != "IDLE":
            _log.info("Cluster %s is %s — %d tasks queued, waiting", cluster_name, cluster_state, len(pending))
            return

        _log.info("Cluster %s is IDLE — executing %d queued tasks", cluster_name, len(pending))

        for r in pending:
            task_config = r.get("queued_task", {})
            task_name = task_config.get("ChatHealthyTask", "?")
            r["status"] = "active"
            r.pop("queued_task", None)
            self._save_state(state)

            _log.info("Executing queued task: %s (reservation: %s)", task_name, r.get("job_id"))
            try:
                # Import and execute the sync task handler
                from function_app import SYNC_TASK_HANDLERS
                handler = SYNC_TASK_HANDLERS.get(task_name)
                if handler:
                    result = handler(task_config.get("payload", {}))
                    _log.info("Queued task %s complete: %s", task_name, result)
                else:
                    _log.error("Unknown queued task: %s", task_name)
            except Exception as e:
                _log.error("Queued task %s failed: %s", task_name, e)
                if self._push:
                    self._push("Queued Task Failed", f"{task_name}: {e}")

    def _get_cluster_state(self, cluster_name: str) -> str:
        """Check current cluster state via Atlas API. Non-blocking."""
        try:
            from atlas_cluster_manager import _get_auth, _get_group_id
            url = f"https://cloud.mongodb.com/api/atlas/v2/groups/{_get_group_id()}/clusters/{cluster_name}"
            resp = requests.get(
                url,
                auth=_get_auth(),
                headers={"Accept": "application/vnd.atlas.2023-02-01+json"},
                timeout=15,
            )
            return resp.json().get("stateName", "UNKNOWN")
        except Exception as e:
            _log.warning("Cluster state check failed: %s", e)
            return "UNKNOWN"

    def check_overdue(self):
        """Check for overdue reservations. Called by the timer trigger.

        Alerts Boss via Pushover if any reservation exceeds expected_end_time.
        """
        now = datetime.now(timezone.utc)
        state = self._get_state()
        overdue = []

        for r in state["reservations"]:
            expected_end = r.get("expected_end_time", "")
            if expected_end:
                try:
                    end_dt = datetime.fromisoformat(expected_end)
                    if now > end_dt:
                        minutes_over = int((now - end_dt).total_seconds() / 60)
                        overdue.append((r, minutes_over))
                except (ValueError, TypeError):
                    pass

        if overdue and self._push:
            for r, minutes in overdue:
                self._push(
                    "Pipeline Overdue",
                    f"Job {r['job_id']} ({r['requester']}) is {minutes} minutes past "
                    f"expected completion ({r['expected_duration_minutes']} min reserved). "
                    f"Cluster {r['cluster_name']} is still running."
                )
                _log.warning("OVERDUE: %s by %d minutes", r["job_id"], minutes)

        # Also check for orphaned reservations (no heartbeat, stale > 4 hours)
        stale_threshold = now - timedelta(hours=4)
        for r in state["reservations"]:
            start = r.get("start_time", "")
            if start:
                try:
                    start_dt = datetime.fromisoformat(start)
                    if start_dt < stale_threshold:
                        _log.warning("STALE reservation: %s started %s", r["job_id"], start)
                        if self._push:
                            self._push(
                                "Stale Pipeline Reservation",
                                f"Job {r['job_id']} has been running for over 4 hours. "
                                f"May be orphaned. Check and release manually if needed."
                            )
                except (ValueError, TypeError):
                    pass

    def get_status(self) -> dict:
        """Return current cluster status for operator visibility."""
        state = self._get_state()
        active = state.get("reservations", [])
        return {
            "cluster_state": "RUNNING" if active else "IDLE",
            "active_reservations": len(active),
            "reservations": active,
            "last_updated": state.get("last_updated", ""),
        }

    def force_release_all(self, cluster_name: str):
        """Manual kill switch. Releases all reservations and shuts down.

        Called by Boss when something is stuck.
        """
        state = self._get_state()
        released = len(state["reservations"])
        state["reservations"] = []
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        state["last_force_release"] = datetime.now(timezone.utc).isoformat()
        self._save_state(state)
        self._shutdown_cluster(cluster_name)
        _log.warning("FORCE RELEASE: cleared %d reservations, shutting down %s", released, cluster_name)

    def _wake_cluster(self, cluster_name: str):
        """Send resume request to Atlas — non-blocking. Does NOT wait for IDLE.

        The 5-minute timer polls cluster state. Work is queued in reservations
        and proceeds when the timer detects IDLE.
        """
        try:
            from atlas_cluster_manager import _atlas_api_patch, _get_auth, _get_group_id
            # Just send the PATCH to unpause — don't wait
            url = f"https://cloud.mongodb.com/api/atlas/v2/groups/{_get_group_id()}/clusters/{cluster_name}"
            resp = requests.patch(
                url,
                json={"paused": False},
                auth=_get_auth(),
                headers={"Content-Type": "application/json", "Accept": "application/vnd.atlas.2023-02-01+json"},
                timeout=30,
            )
            state = resp.json().get("stateName", "unknown")
            _log.info("Cluster %s resume requested (non-blocking). Current state: %s", cluster_name, state)
        except Exception as e:
            _log.error("Failed to request cluster wake for %s: %s", cluster_name, e)
            if self._push:
                self._push("Cluster Wake Failed", f"Could not start {cluster_name}: {e}")

    def _shutdown_cluster(self, cluster_name: str):
        """Pause/stop the Atlas cluster."""
        try:
            from atlas_cluster_manager import pause_cluster
            pause_cluster(cluster_name)
            _log.info("Cluster %s shut down", cluster_name)
        except Exception as e:
            _log.error("Failed to shut down cluster %s: %s", cluster_name, e)
            if self._push:
                self._push("Cluster Shutdown Failed", f"Could not stop {cluster_name}: {e}")
