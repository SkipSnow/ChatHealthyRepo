#!/usr/bin/env python3
# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# unattended_monitor.py — Semaphore-based unattended job monitor.
#
# Pattern:
#   1. Wake up (cron fires every 5 min)
#   2. Check semaphore — if locked, another instance is working. Sleep.
#   3. Acquire semaphore
#   4. Read assignment from brain
#   5. Execute assignment (check pipeline, debug if failed, resubmit)
#   6. Log results
#   7. Release semaphore
#
# Usage:
#   python unattended_monitor.py              (single execution — cron calls this)
#   python unattended_monitor.py --loop       (loop mode — runs every 5 min internally)
#   python unattended_monitor.py --deadline 2026-04-04T14:00:00Z

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("monitor")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SEMAPHORE = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "monitor_semaphore.json"
ASSIGNMENT = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "unattended_assignment.json"
LOG_FILE = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "pipeline_maintenance_log.json"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _append_log(entry):
    data = _read_json(LOG_FILE) or {"log": []}
    data["log"].append(entry)
    _write_json(LOG_FILE, data)


# ── Semaphore ──────────────────────────────────────────────

def acquire_semaphore():
    """Try to acquire. Returns True if acquired, False if another instance holds it."""
    sem = _read_json(SEMAPHORE)
    if sem and sem.get("locked"):
        acquired = sem.get("acquired", "")
        # Stale semaphore — if older than 30 min, force release
        try:
            acquired_dt = datetime.fromisoformat(acquired)
            age_min = (datetime.now(timezone.utc) - acquired_dt).total_seconds() / 60
            if age_min > 30:
                log.warning("Stale semaphore (%.0f min old). Force releasing.", age_min)
            else:
                log.info("Semaphore held by another instance (%.0f min). Sleeping.", age_min)
                return False
        except Exception:
            pass
    _write_json(SEMAPHORE, {"locked": True, "acquired": _now(), "pid": os.getpid()})
    return True


def release_semaphore():
    _write_json(SEMAPHORE, {"locked": False, "released": _now()})


# ── Pipeline Status ────────────────────────────────────────

def check_pipeline(assignment):
    inst = assignment["instructions"]
    instance_id = inst["instance_id"]
    api_url = inst["api_url"]
    token = inst["bearer_token"]

    # Get status
    status_url = (
        f"{api_url}/runtime/webhooks/durabletask/instances/{instance_id}"
        f"?taskHub=DevPipelineManagmentService&connection=Storage"
        f"&code={os.environ.get('DURABLE_TASK_CODE', '')}"
    )
    try:
        r = requests.get(status_url, timeout=15)
        data = r.json()
    except Exception as e:
        return {"status": "error", "message": str(e)}

    status = data.get("runtimeStatus", "unknown")
    custom = data.get("customStatus", "")
    output = data.get("output", "")

    return {
        "status": status,
        "customStatus": custom,
        "output": str(output)[:500] if output else "",
    }


def resubmit_pipeline(assignment, start_step=1):
    """Resubmit the pipeline from a specific step."""
    inst = assignment["instructions"]
    api_url = inst["api_url"]
    token = inst["bearer_token"]

    payload = {
        "ChatHealthyTask": "FindCarePipeline",
        "payload": {
            "states": ["DE", "MS", "VA", "CA"],
            "env_prefix": "dev",
            "start_step": start_step,
        },
    }
    headers = {"Content-Type": "application/json", "Authorization": token}
    try:
        r = requests.post(f"{api_url}/api/Router", json=payload, headers=headers, timeout=30)
        data = r.json()
        new_id = data.get("id", "")
        log.info("Resubmitted pipeline. New instance: %s", new_id)

        # Update assignment with new instance ID
        assignment["instructions"]["instance_id"] = new_id
        _write_json(ASSIGNMENT, assignment)

        return {"resubmitted": True, "instance_id": new_id, "start_step": start_step}
    except Exception as e:
        return {"resubmitted": False, "error": str(e)}


# ── Main execution ─────────────────────────────────────────

def execute_once():
    """One cycle: check semaphore → read assignment → execute → release."""
    # 1. Check semaphore
    if not acquire_semaphore():
        return "skipped_semaphore"

    try:
        # 2. Read assignment
        assignment = _read_json(ASSIGNMENT)
        if not assignment or assignment.get("status") != "active":
            log.info("No active assignment. Releasing.")
            return "no_assignment"

        # 3. Check deadline
        deadline = assignment.get("deadline", "")
        if deadline and _now() > deadline:
            log.info("Deadline passed. Marking assignment complete.")
            assignment["status"] = "deadline_reached"
            _write_json(ASSIGNMENT, assignment)
            _append_log({"time": _now(), "action": "deadline_reached"})
            return "deadline"

        # 4. Execute — check pipeline
        log.info("Checking pipeline status...")
        result = check_pipeline(assignment)
        status = result["status"]
        custom = result.get("customStatus", "")

        log.info("Pipeline: %s | %s", status, custom)
        _append_log({"time": _now(), "status": status, "customStatus": custom})

        if status == "Completed":
            log.info("Pipeline completed successfully!")
            assignment["status"] = "completed"
            _write_json(ASSIGNMENT, assignment)
            _append_log({"time": _now(), "action": "pipeline_completed"})
            return "completed"

        if status == "Running":
            log.info("Pipeline running. Step: %s", custom)
            return "running"

        if status == "Failed":
            error = result.get("output", "")
            log.error("Pipeline FAILED: %s", error[:200])
            _append_log({"time": _now(), "action": "pipeline_failed", "error": error})

            # Count retries
            log_data = _read_json(LOG_FILE) or {"log": []}
            retries = sum(1 for e in log_data["log"] if e.get("action") == "resubmitted")
            if retries >= 3:
                log.error("3 retries exhausted. Stopping for human review.")
                assignment["status"] = "stuck_for_boss"
                _write_json(ASSIGNMENT, assignment)
                _append_log({"time": _now(), "action": "stuck_after_3_retries"})
                return "stuck"

            # Determine start_step from error
            start_step = 1  # default: restart from beginning
            if "Step 0" in custom or "Reserving cluster" in custom:
                start_step = 1  # cluster issue, retry from health check
            elif "Step 1" in custom or "health" in custom.lower():
                start_step = 1
            elif "Step 2" in custom or "Loading" in custom:
                start_step = 2
            elif "Step 3" in custom:
                start_step = 3

            log.info("Resubmitting from step %d (retry %d/3)", start_step, retries + 1)
            resub = resubmit_pipeline(assignment, start_step)
            _append_log({"time": _now(), "action": "resubmitted", **resub})
            return "resubmitted"

        # Unknown status
        log.warning("Unknown status: %s", status)
        _append_log({"time": _now(), "status": status, "action": "unknown_status"})
        return "unknown"

    finally:
        # 5. Release semaphore
        release_semaphore()


def loop(deadline="2026-04-04T14:00:00Z", interval=300):
    """Loop mode — run every interval seconds until deadline."""
    log.info("Starting monitor loop. Deadline: %s, interval: %ds", deadline, interval)
    while True:
        if _now() > deadline:
            log.info("Deadline reached. Exiting loop.")
            break
        result = execute_once()
        log.info("Cycle result: %s", result)
        if result in ("completed", "deadline", "stuck", "no_assignment"):
            break
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run in loop mode")
    parser.add_argument("--deadline", default="2026-04-04T14:00:00Z")
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()

    if args.loop:
        loop(args.deadline, args.interval)
    else:
        execute_once()
