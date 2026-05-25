# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""pipeline_health_and_zombie_kill — Step 1 sub-orchestration.

Two activities, sequential:
  1. ping_mongo_writable_activity  — confirms Mongo is reachable and writable.
  2. kill_zombie_orchestrations_activity — enumerates non-terminal instances
     of the same orchestrator name (e.g., provider_pipeline_orchestrator) and
     terminates every one whose instance_id differs from the current run's.
     Scope is THIS pipeline only; other pipelines' running orchestrations
     are not touched.

Termination uses the Azure Functions Durable management HTTP API exposed on
the host at /runtime/webhooks/durabletask. The master key required to call
it is read from DURABLE_MGMT_CODE in app settings; when unset the activity
logs a warning and returns terminated=0 so the health gate still passes —
the operator must then provision DURABLE_MGMT_CODE.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests


# ── Sub-orchestration ─────────────────────────────────────────────────────────

def pipeline_health_and_zombie_kill_orchestrator_fn(context):
    cfg = context.get_input() or {}
    orchestrator_name = cfg["orchestrator_name"]
    current_instance_id = context.instance_id

    context.set_custom_status("Step 1a: MongoDB health check")
    health = yield context.call_activity("ping_mongo_writable_activity", cfg)

    context.set_custom_status(f"Step 1b: Killing zombies of {orchestrator_name}")
    kill_cfg = {
        **cfg,
        "orchestrator_name":   orchestrator_name,
        "current_instance_id": current_instance_id,
    }
    kill = yield context.call_activity("kill_zombie_orchestrations_activity", kill_cfg)

    return {"health": health, "zombie_kill": kill}


# ── Activities ────────────────────────────────────────────────────────────────

def ping_mongo_writable_fn(config: dict) -> dict:
    """Ping MongoDB and confirm the admin database can accept a write.

    Uses the existing pipeline_health.check_mongo_health for the ping (which
    already raises and emails on failure), then writes a single heartbeat
    document to admin.PipelineHealth to confirm write capability.
    """
    from pipeline_health import check_mongo_health

    ping = check_mongo_health(config)

    from pymongo import MongoClient
    client = MongoClient(
        os.environ["MONGO_connectionString"],
        serverSelectionTimeoutMS=15_000,
    )
    try:
        coll = client["admin"]["PipelineHealth"]
        doc = {
            "checked_at":   datetime.now(timezone.utc).isoformat(),
            "orchestrator": config.get("orchestrator_name"),
            "instance_id":  config.get("current_instance_id"),
            "states":       config.get("states"),
        }
        result = coll.insert_one(doc)
    finally:
        client.close()

    return {
        "ping": ping,
        "heartbeat_id": str(result.inserted_id),
    }


def kill_zombie_orchestrations_fn(config: dict) -> dict:
    """Enumerate non-terminal instances of `orchestrator_name`; terminate every
    one whose instance_id is not `current_instance_id`.

    Scope is exactly one orchestrator name per call — other pipelines'
    orchestrations are not enumerated or touched.
    """
    orchestrator_name = config["orchestrator_name"]
    current = config["current_instance_id"]
    code = os.environ.get("DURABLE_MGMT_CODE")
    site = os.environ.get("WEBSITE_HOSTNAME")

    if not (code and site):
        logging.warning(
            "kill_zombie_orchestrations: DURABLE_MGMT_CODE or WEBSITE_HOSTNAME "
            "unset; skipping zombie scan. orchestrator=%s current=%s",
            orchestrator_name, current,
        )
        return {
            "terminated":    0,
            "targets":       [],
            "orchestrator":  orchestrator_name,
            "warning":       "DURABLE_MGMT_CODE/WEBSITE_HOSTNAME unset",
        }

    base = f"https://{site}/runtime/webhooks/durabletask"
    list_url = (
        f"{base}/instances"
        f"?code={code}"
        "&runtimeStatus=Running,Pending,ContinuedAsNew"
        "&showInput=false"
    )

    resp = requests.get(list_url, timeout=30)
    resp.raise_for_status()
    instances = resp.json() or []

    terminated: list = []
    failures: list = []
    for inst in instances:
        if inst.get("name") != orchestrator_name:
            continue
        iid = inst.get("instanceId")
        if not iid or iid == current:
            continue
        term_url = (
            f"{base}/instances/{iid}/terminate"
            f"?code={code}"
            f"&reason=zombie-killed-by-{current}"
        )
        r = requests.post(term_url, timeout=30)
        if r.status_code in (200, 202):
            terminated.append(iid)
        else:
            failures.append({"instance_id": iid, "status": r.status_code, "body": r.text[:200]})
            logging.warning(
                "kill_zombie_orchestrations: terminate failed for %s: %d %s",
                iid, r.status_code, r.text[:200],
            )

    logging.info(
        "kill_zombie_orchestrations: orchestrator=%s terminated=%d failures=%d",
        orchestrator_name, len(terminated), len(failures),
    )
    return {
        "terminated":   len(terminated),
        "targets":      terminated,
        "failures":     failures,
        "orchestrator": orchestrator_name,
    }
