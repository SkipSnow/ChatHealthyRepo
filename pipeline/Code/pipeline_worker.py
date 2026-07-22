"""pipeline_worker.py — v32 §3.1.5 / §4.3.3 / §5.2 — Worker subprocess entrypoint.

Invoked by the Controller via subprocess.Popen:

    python -m pipeline_worker <step_name> --replica <i> --run-id <run_id>

Contract (v32 §4.3.4):
  1. Atomically claim ONE work-item from chathealthyfrontend.pipeline.work_items
     via findOneAndUpdate on
       {run_id, step, status: "pending"}
     sorted by created_at ASC. Two Workers never claim the same document.
  2. Start a heartbeat thread that updates heartbeat_at every 120 seconds
     while the step is running.
  3. Dispatch on step name (registered handlers below).
  4. On completion, write result + finished_at + status.done and exit 0.
     On failure, write status.failed with an error dict, exit 1.

Local runs invoke this same module (Controller may spawn Workers locally
for smoke testing).
"""
from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

import argparse
import datetime
import json
import os
import socket
import sys
import threading
import time
import traceback

from pipeline_db import get_mongo

_log = ChatHealthyLoggingService()

HEARTBEAT_INTERVAL_S = 120     # v32 §4.3.4
WORK_ITEMS_COLLECTION = "pipeline.work_items"
FRONTEND_DB = "chathealthyfrontend"


# ─────────────────────────────────────────────────────────────────────────────
# Step handler registry — one function per step name in v32 §5.2.
# Handlers receive (payload: dict) and return an `output` dict written back
# to the work-item on success.
#
# Handlers live in their own modules; this registry is imported lazily to
# avoid pulling every step's dependencies into every Worker process.
# ─────────────────────────────────────────────────────────────────────────────
def _dispatch(step: str, payload: dict) -> dict:
    """Route the step to its handler. Returns handler output dict."""
    handlers = {
        # These will be populated as each step's handler module is authored.
        # For the initial v32 hello-world: only "connectivity_probe" exists.
        "connectivity_probe":       _handle_connectivity_probe,
    }
    fn = handlers.get(step)
    if fn is None:
        raise NotImplementedError(
            f"pipeline_worker: no handler registered for step {step!r}. "
            f"Register in pipeline_worker._dispatch()."
        )
    return fn(payload)


def _handle_connectivity_probe(payload: dict) -> dict:
    """Smoke-test handler: prove the Worker can talk to Mongo. Writes a
    small heartbeat doc and returns."""
    mongo = get_mongo()
    mongo.admin.command("ping")
    return {
        "probed_at": datetime.datetime.utcnow().isoformat() + "Z",
        "host": socket.gethostname(),
        "payload_echo": payload,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Claim primitive — atomic findOneAndUpdate per v32 §4.3.4
# ─────────────────────────────────────────────────────────────────────────────
def _claim_work_item(mongo, run_id: str, step: str, worker_pid: int) -> dict | None:
    coll = mongo[FRONTEND_DB][WORK_ITEMS_COLLECTION]
    now = datetime.datetime.utcnow()
    return coll.find_one_and_update(
        {"run_id": run_id, "step": step, "status": "pending"},
        {"$set": {
            "status": "running",
            "claimed_by": worker_pid,
            "claimed_at": now,
            "heartbeat_at": now,
        }},
        sort=[("created_at", 1)],
        return_document=True,   # pymongo ReturnDocument.AFTER
    )


def _write_heartbeat(mongo, item_id) -> None:
    coll = mongo[FRONTEND_DB][WORK_ITEMS_COLLECTION]
    coll.update_one(
        {"_id": item_id},
        {"$set": {"heartbeat_at": datetime.datetime.utcnow()}},
    )


def _mark_done(mongo, item_id, output: dict) -> None:
    coll = mongo[FRONTEND_DB][WORK_ITEMS_COLLECTION]
    coll.update_one(
        {"_id": item_id},
        {"$set": {
            "status": "done",
            "output": output,
            "finished_at": datetime.datetime.utcnow(),
        }},
    )


def _mark_failed(mongo, item_id, error: dict) -> None:
    coll = mongo[FRONTEND_DB][WORK_ITEMS_COLLECTION]
    coll.update_one(
        {"_id": item_id},
        {"$set": {
            "status": "failed",
            "output": {"error": error},
            "finished_at": datetime.datetime.utcnow(),
        }},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat thread — 120-second cadence per v32 §4.3.4
# ─────────────────────────────────────────────────────────────────────────────
class _HeartbeatThread(threading.Thread):
    def __init__(self, mongo, item_id):
        super().__init__(daemon=True)
        self._mongo = mongo
        self._item_id = item_id
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            try:
                _write_heartbeat(self._mongo, self._item_id)
            except Exception as exc:  # noqa: BLE001
                _log.warning("heartbeat write failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pipeline Worker subprocess")
    p.add_argument("step", help="Step name (matches STEPS[].name in the orchestrator)")
    p.add_argument("--replica", type=int, default=0)
    p.add_argument("--run-id", dest="run_id",
                   default=os.environ.get("RUN_ID", ""),
                   help="Run id (also read from RUN_ID env)")
    p.add_argument("--log-level", dest="log_level",
                   default=os.environ.get("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv if argv is not None else sys.argv[1:])
    if ns.log_level:
        os.environ.setdefault("LOG_LEVEL", ns.log_level.upper())

    if not ns.run_id:
        _log.error("pipeline_worker: --run-id or RUN_ID env is required")
        return 2

    mongo = get_mongo()
    worker_pid = os.getpid()

    item = _claim_work_item(mongo, ns.run_id, ns.step, worker_pid)
    if item is None:
        _log.info("pipeline_worker: no pending work-item for run_id=%s step=%s — exiting cleanly",
                  ns.run_id, ns.step)
        return 0

    _log.info("pipeline_worker: claimed item _id=%s run_id=%s step=%s payload=%s",
              item.get("_id"), ns.run_id, ns.step, item.get("payload"))

    heartbeat = _HeartbeatThread(mongo, item["_id"])
    heartbeat.start()
    try:
        output = _dispatch(ns.step, item.get("payload") or {})
        _mark_done(mongo, item["_id"], output)
        _log.info("pipeline_worker: done item _id=%s output_keys=%s",
                  item["_id"], sorted(output.keys()))
        return 0
    except Exception as exc:  # noqa: BLE001
        err = {
            "type": type(exc).__name__,
            "msg": str(exc),
            "traceback": traceback.format_exc()[-2000:],
        }
        _mark_failed(mongo, item["_id"], err)
        _log.error("pipeline_worker: failed item _id=%s: %s", item["_id"], err["msg"])
        return 1
    finally:
        heartbeat.stop()


if __name__ == "__main__":
    raise SystemExit(main())
