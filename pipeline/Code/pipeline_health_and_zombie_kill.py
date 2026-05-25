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
    # current_load_id is the parent's instance_id, propagated by every
    # sub-orch config so the zombie scan can match by input.load_id.
    current_load_id = cfg.get("current_load_id")

    context.set_custom_status("Step 1a: MongoDB health check")
    health = yield context.call_activity("ping_mongo_writable_activity", cfg)

    context.set_custom_status(f"Step 1b: Killing zombies of {orchestrator_name}")
    kill_cfg = {
        **cfg,
        "orchestrator_name": orchestrator_name,
        "current_load_id":   current_load_id,
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
    """Terminate every non-terminal orchestration that belongs to a prior run
    of THIS pipeline.

    Linkage is the parent orchestration's instance_id, propagated as
    ``load_id`` through every sub-orch config. The scan uses the Durable
    management API's ``showInput=true`` to expose each instance's input, then
    matches ``input.load_id`` to determine ownership.

    Two pass match:
      1. Parent-level zombies: instance.name == orchestrator_name AND
         instance_id != current_load_id.
      2. Sub-orch zombies: instance.input.load_id is set AND
         instance.input.load_id != current_load_id.

    The current run is preserved in both passes (current_load_id == current
    parent instance_id). Other pipelines' orchestrations (without a load_id
    field of their own) are not touched.
    """
    import json as _json

    orchestrator_name = config["orchestrator_name"]
    current_load_id = config.get("current_load_id") or ""
    code = os.environ.get("DURABLE_MGMT_CODE")
    site = os.environ.get("WEBSITE_HOSTNAME")

    if not (code and site):
        logging.warning(
            "kill_zombie_orchestrations: DURABLE_MGMT_CODE or WEBSITE_HOSTNAME "
            "unset; skipping zombie scan. orchestrator=%s current=%s",
            orchestrator_name, current_load_id,
        )
        return {
            "terminated":   0,
            "targets":      [],
            "orchestrator": orchestrator_name,
            "warning":      "DURABLE_MGMT_CODE/WEBSITE_HOSTNAME unset",
        }

    base = f"https://{site}/runtime/webhooks/durabletask"
    list_url = (
        f"{base}/instances"
        f"?code={code}"
        "&runtimeStatus=Running,Pending,ContinuedAsNew"
        "&showInput=true"
    )

    resp = requests.get(list_url, timeout=60)
    resp.raise_for_status()
    instances = resp.json() or []

    def _input_load_id(inst: dict) -> str | None:
        raw = inst.get("input")
        if raw is None:
            return None
        if isinstance(raw, dict):
            v = raw.get("load_id")
            return v if isinstance(v, str) and v else None
        if isinstance(raw, str):
            try:
                d = _json.loads(raw)
            except Exception:
                return None
            if isinstance(d, dict):
                v = d.get("load_id")
                return v if isinstance(v, str) and v else None
        return None

    targets: list = []
    for inst in instances:
        iid = inst.get("instanceId")
        if not iid or iid == current_load_id:
            continue
        if inst.get("name") == orchestrator_name:
            targets.append({"instance_id": iid, "name": inst.get("name"), "match": "parent_name"})
            continue
        inp_load = _input_load_id(inst)
        if inp_load and inp_load != current_load_id:
            targets.append({"instance_id": iid, "name": inst.get("name"),
                            "match": "input_load_id", "owner_load_id": inp_load})

    terminated: list = []
    failures: list = []
    for t in targets:
        iid = t["instance_id"]
        term_url = (
            f"{base}/instances/{iid}/terminate"
            f"?code={code}"
            f"&reason=zombie-killed-by-{current_load_id}"
        )
        r = requests.post(term_url, timeout=30)
        if r.status_code in (200, 202):
            terminated.append(t)
        else:
            failures.append({**t, "status": r.status_code, "body": r.text[:200]})
            logging.warning(
                "kill_zombie_orchestrations: terminate failed for %s: %d %s",
                iid, r.status_code, r.text[:200],
            )

    logging.info(
        "kill_zombie_orchestrations: orchestrator=%s candidates=%d terminated=%d failures=%d",
        orchestrator_name, len(targets), len(terminated), len(failures),
    )
    return {
        "terminated":   len(terminated),
        "targets":      terminated,
        "failures":     failures,
        "orchestrator": orchestrator_name,
        "current_load_id": current_load_id,
    }
