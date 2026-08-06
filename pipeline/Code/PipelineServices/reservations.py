# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Reservation lookup for pipeline tiers.

The runbook is the pipeline gatekeeper: before spawning Control it MUST
check whether any job is currently running (holding a reservation on
the pipeline cluster). The canonical source of truth is
admin.cluster_lifecycle on the front cluster; each active reservation
is one doc with status='active' and cluster_name = the pipeline
cluster. Stale reservations get cleared by reservation_reaper.

This lib only provides read-side lookup. Acquire/release live in
cluster_lifecycle_manager.py and are called by Controller/Worker (which
own the reservations they create).
"""
from __future__ import annotations
from pipeline_db import METADATA_DB


def find_active_reservations(mongo_client, cluster_name: str) -> list:
    """Return every active reservation doc on cluster_name from
    admin.cluster_lifecycle. Empty list means no job is currently
    running -- the runbook is free to spawn Control."""
    coll = mongo_client[METADATA_DB]["cluster_lifecycle"]
    return list(coll.find({
        "cluster_name": cluster_name,
        "status": "active",
    }))
