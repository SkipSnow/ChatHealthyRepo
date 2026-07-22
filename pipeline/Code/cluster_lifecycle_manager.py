from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException
# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ClusterLifecycleManager — Infrastructure operations manager.
# Manages MongoDB Atlas cluster lifecycle. No pipeline business logic.
#
# Design: FindCarePipeline, Pipeline Operations Manager
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

import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta

import requests
from pymongo.errors import DuplicateKeyError
from requests.auth import HTTPDigestAuth

_log = ChatHealthyLoggingService()


# ── Reservation Model ────────────────────────────────────────

@dataclass
class ResourceReservation:
    """The interface between Ops Manager and Dev Manager.

    Dev Manager creates a reservation before starting work.
    Dev Manager releases in finally. Ops Manager manages the cluster.

    Live-only data store: rows exist while a job (or human) holds the cluster
    and are deleted on release. No 'released' state is persisted; the only
    value `status` ever holds is "active". Forensic / audit needs are served
    by Atlas database auditing, Azure Activity Log, and Application Insights.
    """
    job_id: str
    requester: str
    cluster_name: str
    expected_duration_minutes: int      # max — OVERDUE if exceeded
    expected_min_minutes: int = 0       # min — WARNING if completed faster
    start_time: str = ""
    expected_end_time: str = ""
    status: str = "active"
    # Runtime classification on the live row only. The 5-min ops timer reaps
    # overdue automated rows by deletion; human-class rows are NEVER auto-reaped.
    reservation_class: str = "automated"  # "automated" | "human"

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

    def __init__(self, get_db_fn, env_prefix: str = "", push_fn=None):
        self._get_db = get_db_fn
        self._env = env_prefix
        self._push = push_fn

    def _coll(self):
        client = self._get_db()
        return client["admin"]["cluster_lifecycle"] if client is not None else None

    def wake(self, cluster_name: str, job_id: str | None = None) -> None:
        try:
            resp = requests.patch(
                _atlas_url(cluster_name),
                json={"paused": False},
                auth=_atlas_auth(),
                headers=_atlas_headers(),
                timeout=30,
            )
            state_name = resp.json().get("stateName", "unknown")
            _log.info("Wake requested: %s by job=%s (state: %s)", cluster_name, job_id, state_name)
        except Exception as e:
            _log.error("Wake failed: %s by job=%s — %s", cluster_name, job_id, e)
            if self._push:
                self._push("Cluster Wake Failed", f"{cluster_name} (job={job_id}): {e}")

    def reserve(self, cluster_name: str, job_id: str, requester: str,
                expected_duration_minutes: int, expected_min_minutes: int = 0,
                reservation_class: str = "automated") -> dict:
        if reservation_class not in ("automated", "human"):
            raise ChatHealthyException(mode="value_error", message=f"reservation_class must be 'automated' or 'human', got {reservation_class!r}")
        now = datetime.now(timezone.utc)
        doc = {
            "_id": job_id,
            "job_id": job_id,
            "requester": requester,
            "cluster_name": cluster_name,
            "expected_duration_minutes": expected_duration_minutes,
            "expected_min_minutes": expected_min_minutes,
            "start_time": now.isoformat(),
            "expected_end_time": (now + timedelta(minutes=expected_duration_minutes)).isoformat(),
            "status": "active",
            "reservation_class": reservation_class,
        }
        coll = self._coll()
        if coll is None:
            return doc
        try:
            coll.insert_one(doc)
        except DuplicateKeyError:
            pass
        return doc

    # ── v0.1 API: ClusterStatus ──────────────────────────────

    def status(self, cluster_name: str, job_id: str | None = None) -> dict:
        cluster_state = self._get_cluster_state(cluster_name)
        coll = self._coll()
        active = list(coll.find({"cluster_name": cluster_name})) if coll is not None else []
        if job_id is not None:
            _log.info("Status check: %s by job=%s (state: %s, active=%d)",
                      cluster_name, job_id, cluster_state, len(active))
        return {
            "cluster_name": cluster_name,
            "cluster_state": cluster_state,
            "active_reservations": len(active),
            "reservations": active,
        }

    # ── v0.1 API: Release ────────────────────────────────────

    def release(self, job_id: str) -> dict:
        coll = self._coll()
        if coll is None:
            return {"released": job_id, "deleted_count": 0}
        result = coll.delete_one({"_id": job_id})
        if result.deleted_count:
            _log.info("Released: %s", job_id)
        else:
            _log.warning("Release called for unknown job_id: %s", job_id)
        return {"released": job_id, "deleted_count": result.deleted_count}

    # ── Timer: ops only ──────────────────────────────────────

    def check_overdue(self):
        coll = self._coll()
        if coll is None:
            return
        now = datetime.now(timezone.utc)
        for r in coll.find({}):
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

    # ── Kill switch ──────────────────────────────────────────

    def force_release_all(self, cluster_name: str) -> dict:
        """human kill switch. Release everything, shut down."""
        coll = self._coll()
        if coll is None:
            return {"released": 0, "cluster": cluster_name}
        result = coll.delete_many({"cluster_name": cluster_name})
        self._send_pause(cluster_name)
        _log.warning("FORCE RELEASE: %d reservations cleared, %s shutting down",
                     result.deleted_count, cluster_name)
        return {"released": result.deleted_count, "cluster": cluster_name}

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
