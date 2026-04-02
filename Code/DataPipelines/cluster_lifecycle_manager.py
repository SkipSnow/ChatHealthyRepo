# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ClusterLifecycleManager — Infrastructure operations manager.
# Manages MongoDB Atlas cluster lifecycle. No pipeline business logic.
#
# Design: EPIC-3, Pipeline Operations Manager
# Pattern: Ops Manager owns infrastructure. Dev Manager owns pipeline logic.
# Interface: reserve(cluster, duration) → reservation_id / release(reservation_id)
#
# v0.1 API:
#   WakeCluster  — non-blocking, returns reservation_id
#   ClusterStatus — returns cluster state + reservation list
#   Release — release reservation, shut down if last one
#
# Timer (5 min): check overdue, shut down idle. NO task execution.

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta

import requests
from requests.auth import HTTPDigestAuth

_log = logging.getLogger("ops.cluster_lifecycle")


# ── Reservation Model ────────────────────────────────────────

@dataclass
class ResourceReservation:
    """The interface between Ops Manager and Dev Manager.

    Dev Manager creates a reservation before starting work.
    Dev Manager releases in finally. Ops Manager manages the cluster.
    """
    job_id: str
    requester: str
    cluster_name: str
    expected_duration_minutes: int      # max — OVERDUE if exceeded
    expected_min_minutes: int = 0       # min — WARNING if completed faster
    start_time: str = ""
    expected_end_time: str = ""
    status: str = "active"  # active | released

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ResourceReservation":
        return ResourceReservation(**{k: v for k, v in d.items()
                                      if k in ResourceReservation.__dataclass_fields__})


# ── Atlas API Helpers ────────────────────────────────────────

def _atlas_auth():
    return HTTPDigestAuth(
        os.environ.get("ATLAS_PUBLIC_KEY", ""),
        os.environ.get("ATLAS_PRIVATE_KEY", ""),
    )

def _atlas_group_id():
    return os.environ.get("ATLAS_PROJECT_ID", os.environ.get("ATLAS_GROUP_ID", ""))

def _atlas_url(cluster_name: str) -> str:
    return f"https://cloud.mongodb.com/api/atlas/v2/groups/{_atlas_group_id()}/clusters/{cluster_name}"

def _atlas_headers():
    return {
        "Content-Type": "application/json",
        "Accept": "application/vnd.atlas.2023-02-01+json",
    }


# ── Manager ──────────────────────────────────────────────────

class ClusterLifecycleManager:
    """Infrastructure operations manager for MongoDB Atlas clusters.

    Manages: cluster state, reservations, cost, uptime.
    Never touches: pipeline data, task logic, collection schemas.
    """

    def __init__(self, get_db_fn, env_prefix: str, push_fn=None):
        self._get_db = get_db_fn
        self._env = env_prefix
        self._push = push_fn
        self._coll_name = f"{env_prefix}_System"
        self._doc_id = "cluster_reservations"

    # ── State persistence ────────────────────────────────────

    def _get_state(self) -> dict:
        db = self._get_db()
        if db is None:
            return {"_id": self._doc_id, "reservations": []}
        doc = db[self._coll_name]["cluster_lifecycle"].find_one({"_id": self._doc_id})
        if not doc:
            doc = {"_id": self._doc_id, "reservations": []}
            db[self._coll_name]["cluster_lifecycle"].insert_one(doc)
        return doc

    def _save_state(self, state: dict):
        db = self._get_db()
        if db is None:
            return
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        db[self._coll_name]["cluster_lifecycle"].replace_one(
            {"_id": self._doc_id}, state, upsert=True
        )

    # ── v0.1 API: WakeCluster ────────────────────────────────

    def reserve(self, cluster_name: str, job_id: str, requester: str,
                expected_duration_minutes: int, expected_min_minutes: int = 0) -> dict:
        """Reserve a cluster. Sends wake request (non-blocking). Returns reservation.

        Called by pipeline tasks before starting work.
        The caller must poll status() until cluster is IDLE before doing work.
        """
        now = datetime.now(timezone.utc)
        reservation = ResourceReservation(
            job_id=job_id,
            requester=requester,
            cluster_name=cluster_name,
            expected_duration_minutes=expected_duration_minutes,
            expected_min_minutes=expected_min_minutes,
            start_time=now.isoformat(),
            expected_end_time=(now + timedelta(minutes=expected_duration_minutes)).isoformat(),
            status="active",
        )

        state = self._get_state()
        state["reservations"].append(reservation.to_dict())
        self._save_state(state)

        _log.info("Reservation: %s by %s (%d min)", job_id, requester, expected_duration_minutes)

        # Wake cluster — non-blocking, fire and forget
        self._send_wake(cluster_name)

        return reservation.to_dict()

    # ── v0.1 API: ClusterStatus ──────────────────────────────

    def status(self, cluster_name: str) -> dict:
        """Return cluster state and active reservations.

        Called by pipeline tasks polling for IDLE before starting work.
        """
        cluster_state = self._get_cluster_state(cluster_name)
        state = self._get_state()
        active = [r for r in state["reservations"] if r.get("cluster_name") == cluster_name]

        return {
            "cluster_name": cluster_name,
            "cluster_state": cluster_state,
            "active_reservations": len(active),
            "reservations": active,
            "last_updated": state.get("last_updated", ""),
        }

    # ── v0.1 API: Release ────────────────────────────────────

    def release(self, job_id: str) -> dict:
        """Release a reservation. Shuts down cluster if last one.

        Called in pipeline finally block. Always called — success or failure.
        """
        state = self._get_state()

        # Find the reservation to get cluster_name
        released = None
        remaining = []
        for r in state["reservations"]:
            if r.get("job_id") == job_id:
                released = r
            else:
                remaining.append(r)

        state["reservations"] = remaining
        self._save_state(state)

        if released:
            _log.info("Released: %s", job_id)
            cluster_name = released.get("cluster_name", "")
            # Shut down if no more reservations for this cluster
            cluster_reservations = [r for r in remaining if r.get("cluster_name") == cluster_name]
            if not cluster_reservations:
                self._send_pause(cluster_name)
        else:
            _log.warning("Release called for unknown job_id: %s", job_id)

        return {"released": job_id, "remaining": len(remaining)}

    # ── Timer: ops only ──────────────────────────────────────

    def check_overdue(self):
        """Called by 5-minute timer. Ops work only — no task execution."""
        now = datetime.now(timezone.utc)
        state = self._get_state()

        for r in state["reservations"]:
            expected_end = r.get("expected_end_time", "")
            if not expected_end:
                continue
            try:
                end_dt = datetime.fromisoformat(expected_end)
                if now > end_dt:
                    minutes_over = int((now - end_dt).total_seconds() / 60)
                    _log.warning("OVERDUE: %s by %d min", r["job_id"], minutes_over)
                    if self._push:
                        self._push(
                            "Pipeline Overdue",
                            f"Job {r['job_id']} ({r['requester']}) is {minutes_over} min "
                            f"past expected. Cluster {r['cluster_name']} still running."
                        )
            except (ValueError, TypeError):
                pass

    def check_idle_shutdown(self):
        """Called by 5-minute timer. Shut down clusters with zero reservations."""
        state = self._get_state()
        if not state["reservations"]:
            return  # nothing to check

        # Group by cluster
        clusters = set(r.get("cluster_name", "") for r in state["reservations"])
        for cluster in clusters:
            cluster_res = [r for r in state["reservations"] if r.get("cluster_name") == cluster]
            if not cluster_res:
                _log.info("No reservations for %s — shutting down", cluster)
                self._send_pause(cluster)

    # ── Kill switch ──────────────────────────────────────────

    def force_release_all(self, cluster_name: str) -> dict:
        """Boss kill switch. Release everything, shut down."""
        state = self._get_state()
        released = len(state["reservations"])
        state["reservations"] = [r for r in state["reservations"]
                                  if r.get("cluster_name") != cluster_name]
        state["last_force_release"] = datetime.now(timezone.utc).isoformat()
        self._save_state(state)
        self._send_pause(cluster_name)
        _log.warning("FORCE RELEASE: %d reservations cleared, %s shutting down", released, cluster_name)
        return {"released": released, "cluster": cluster_name}

    # ── Atlas operations (private) ───────────────────────────

    def _send_wake(self, cluster_name: str):
        """Send resume request — non-blocking. Does NOT wait for IDLE."""
        try:
            resp = requests.patch(
                _atlas_url(cluster_name),
                json={"paused": False},
                auth=_atlas_auth(),
                headers=_atlas_headers(),
                timeout=30,
            )
            state_name = resp.json().get("stateName", "unknown")
            _log.info("Wake requested: %s (state: %s)", cluster_name, state_name)
        except Exception as e:
            _log.error("Wake failed: %s — %s", cluster_name, e)
            if self._push:
                self._push("Cluster Wake Failed", f"{cluster_name}: {e}")

    def _send_pause(self, cluster_name: str):
        """Send pause request — non-blocking."""
        try:
            resp = requests.patch(
                _atlas_url(cluster_name),
                json={"paused": True},
                auth=_atlas_auth(),
                headers=_atlas_headers(),
                timeout=30,
            )
            state_name = resp.json().get("stateName", "unknown")
            _log.info("Pause requested: %s (state: %s)", cluster_name, state_name)
        except Exception as e:
            _log.error("Pause failed: %s — %s", cluster_name, e)
            if self._push:
                self._push("Cluster Pause Failed", f"{cluster_name}: {e}")

    def _get_cluster_state(self, cluster_name: str) -> str:
        """Check cluster state — non-blocking."""
        try:
            resp = requests.get(
                _atlas_url(cluster_name),
                auth=_atlas_auth(),
                headers=_atlas_headers(),
                timeout=15,
            )
            return resp.json().get("stateName", "UNKNOWN")
        except Exception as e:
            _log.warning("State check failed: %s — %s", cluster_name, e)
            return "UNKNOWN"
