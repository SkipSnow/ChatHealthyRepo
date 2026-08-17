"""pipeline_worker.py — v32 §3.1.5 / §4.3.3 / §5.2 — Worker subprocess entrypoint.

Invoked by the Controller via subprocess.Popen:

    python -m pipeline_worker <step_name> --replica <i> --run-id <run_id>

Contract (v32 §4.3.4, revised 2026-08-03 to move coord off frontend cluster):
  1. Atomically claim ONE work-item from pipelineAdmin.pipeline.work_items
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
from chathealthy_lib.logging_service import (
    ChatHealthyLoggingService, set_mongo_log_identity)

# This process inherits CH_LOG_DESTINATION=mongo from the container, so it must
# name its own identity. It is a separate process from the Controller; nothing
# the Controller sets in memory survives the spawn.
set_mongo_log_identity("pipelineEditor")

import argparse
import datetime
import json
import os
import socket
import sys
import threading
import time
import traceback

from blob_client import get_blob_service
from step_context import PipelineArgs, RunManifest, StepContext, StepTransition
from steps import get_runner
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

_log = ChatHealthyLoggingService()

HEARTBEAT_INTERVAL_S = 120     # v32 §4.3.4
# The coordination substrate the Controller actually writes to. It enqueues
# every work item with ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")["pipelineAdmin"], and this read
# the database "chathealthypipelines" through get_mongo() -- a different
# database on a different accessor. Workers therefore claimed nothing, logged
# nothing after startup, and the Controller waited on "remaining=1" until it
# timed out. Both sides must name the same place, so they name it once.


# ─────────────────────────────────────────────────────────────────────────────
# Step dispatch — via steps.get_runner(step_name).
# No per-step handler table: every step registered in steps/__init__.py's
# STEP_RUNNERS resolves via get_runner(step_name) and receives a
# reconstructed StepContext.
# ─────────────────────────────────────────────────────────────────────────────
def _reconstruct_step_context(payload: dict, mongo, blob) -> StepContext:
    """Rebuild a StepContext from the work_item payload the Controller
    enqueued. Payload shape (see BasePipelineOrchestrator._invoke_process_pool):
      {run_id, step, partition, config, args, pipeline_name}
    """
    args_snap = payload.get("args") or {}
    args = PipelineArgs(
        states=list(args_snap.get("state_scope") or []),
        env_prefix=args_snap.get("env_prefix", "dev"),
        state_scope=list(args_snap.get("state_scope") or []) or None,
        run_id=payload.get("run_id"),
        resume_from_step=args_snap.get("resume_from_step") or None,
        incremental=(args_snap.get("load_mode") == "incremental"),
        data_version=int(args_snap.get("data_version") or 0),
        # Operator-driven flags round-trip through the payload snapshot.
        google_maps_enabled=bool(args_snap.get("google_maps_enabled", False)),
    )
    pipeline_name = payload.get("pipeline_name", "provider")
    manifest = RunManifest(
        run_id=payload["run_id"],
        pipeline_name=pipeline_name,
        env_prefix=args.env_prefix,
    )
    # Rehydrate manifest fields the Worker's step reads (source_versions,
    # metrics, completed_steps) from the live pipeline.runs doc so per-step
    # decisions that depend on earlier steps' state work identically to
    # inline execution. Coord (pipeline.runs) lives on the pipeline cluster.
    coord = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")
    live = coord["pipelineAdmin"]["pipeline.runs"].find_one({"run_id": payload["run_id"]})
    if live:
        manifest.source_versions = live.get("source_versions") or {}
        manifest.metrics = live.get("metrics") or {}
        manifest.completed_steps = set(live.get("completed_steps") or [])
        manifest.status = live.get("status") or "running"
    config = dict(payload.get("config") or {})
    config["partition"] = payload.get("partition") or {}
    return StepContext(
        args=args,
        manifest=manifest,
        config=config,
        mongo_client=mongo,
        blob_client=blob,
    )


def _dispatch(step: str, payload: dict) -> dict:
    """Route the step to its handler. Every process_pool step in
    provider_pipeline_orchestrator.STEPS resolves through steps.get_runner
    to the module's run_step(ctx) callable."""
    mongo = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyDataPipelines")
    blob = get_blob_service()
    ctx = _reconstruct_step_context(payload, mongo, blob)
    runner = get_runner(step)
    _log.info(
        "pipeline_worker dispatch step=%s run_id=%s partition=%s",
        step, payload.get("run_id"), payload.get("partition"),
    )
    result = runner(ctx)
    output: dict = result if isinstance(result, dict) else {"result": result}
    # Persist per-Worker manifest deltas so serial follow-on steps in the
    # Controller can read them (Worker's manifest object is process-local).
    deltas: dict = {}
    if ctx.manifest.source_versions:
        deltas["source_versions"] = ctx.manifest.source_versions
    if ctx.manifest.metrics:
        deltas["metrics"] = ctx.manifest.metrics
    if deltas:
        set_ops = {}
        for k, v in (deltas.get("source_versions") or {}).items():
            set_ops[f"source_versions.{k}"] = v
        for k, v in (deltas.get("metrics") or {}).items():
            # Nested dict (e.g. metrics.fetch_results): $set the LEAF keys
            # individually so parallel Workers don't overwrite each other's
            # sibling contributions. Prior code $set metrics.fetch_results
            # to this Worker's single-source dict, clobbering the other
            # Workers' fetch_results and causing load_staging Workers to
            # see "no fetch_result on manifest" for 4 of 5 sources.
            if isinstance(v, dict) and v:
                for inner_k, inner_v in v.items():
                    set_ops[f"metrics.{k}.{inner_k}"] = inner_v
            else:
                set_ops[f"metrics.{k}"] = v
        if set_ops:
            # pipeline.runs lives on the pipeline cluster.
            ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")["pipelineAdmin"]["pipeline.runs"].update_one(
                {"run_id": payload["run_id"]},
                {"$set": set_ops},
            )
    return output


# ─────────────────────────────────────────────────────────────────────────────
# Claim primitive — atomic findOneAndUpdate per v32 §4.3.4
# ─────────────────────────────────────────────────────────────────────────────
def _claim_work_item(mongo, run_id: str, step: str, worker_pid: int) -> dict | None:
    coll = mongo["pipelineAdmin"]["pipeline.work_items"]
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
    coll = mongo["pipelineAdmin"]["pipeline.work_items"]
    coll.update_one(
        {"_id": item_id},
        {"$set": {"heartbeat_at": datetime.datetime.utcnow()}},
    )


def _mark_done(mongo, item_id, output: dict) -> None:
    coll = mongo["pipelineAdmin"]["pipeline.work_items"]
    coll.update_one(
        {"_id": item_id},
        {"$set": {
            "status": "done",
            "output": output,
            "finished_at": datetime.datetime.utcnow(),
        }},
    )


def _mark_failed(mongo, item_id, error: dict) -> None:
    coll = mongo["pipelineAdmin"]["pipeline.work_items"]
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

    # work_items live where the Controller enqueues them, which is the
    # front cluster. Reading them anywhere else claims nothing.
    coord = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")
    worker_pid = os.getpid()

    # One look, by design. The Controller does not spawn a worker until every
    # item is readable, so a claim that finds nothing means the work is taken,
    # not that it has yet to appear.
    item = _claim_work_item(coord, ns.run_id, ns.step, worker_pid)
    if item is None:
        # Not "exiting cleanly" -- there is nothing clean about a worker the
        # Controller is waiting on finding no work. Say what was searched for
        # and where, because the previous message left the Controller
        # reporting remaining=1 with no explanation anywhere.
        pending = coord["pipelineAdmin"]["pipeline.work_items"].count_documents(
            {"run_id": ns.run_id})
        unclaimed = coord["pipelineAdmin"]["pipeline.work_items"].count_documents(
            {"run_id": ns.run_id, "step": ns.step, "status": "pending"})
        _log.warning(
            "pipeline_worker: NO work-item claimed. run_id=%s step=%s "
            "db=%s coll=%s items_for_this_run=%d still_pending_for_this_step=%d. "
            "The Controller is waiting on a claim that will never come.",
            ns.run_id, ns.step,
            "pipelineAdmin", "pipeline.work_items", pending, unclaimed,
        )
        return 0

    _log.info("pipeline_worker: claimed item _id=%s run_id=%s step=%s payload=%s",
              item.get("_id"), ns.run_id, ns.step, item.get("payload"))

    heartbeat = _HeartbeatThread(coord, item["_id"])
    heartbeat.start()
    try:
        output = _dispatch(ns.step, item.get("payload") or {})
        _mark_done(coord, item["_id"], output)
        _log.info("pipeline_worker: done item _id=%s output_keys=%s",
                  item["_id"], sorted(output.keys()))
        return 0
    except Exception as exc:  # noqa: BLE001
        err = {
            "type": type(exc).__name__,
            "msg": str(exc),
            "traceback": traceback.format_exc()[-2000:],
        }
        _mark_failed(coord, item["_id"], err)
        _log.error("pipeline_worker: failed item _id=%s: %s", item["_id"], err["msg"])
        return 1
    finally:
        heartbeat.stop()


if __name__ == "__main__":
    raise SystemExit(main())
