# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""work_manager_entity - the single durable actor for the records-as-messages pipeline.

Owns the implicit assignment queue (derived arithmetically from CSV size + chunk_size),
the worker roster, the spawn counter (monotonic, never reused), and the discrepancy
counter. Spawns and respawns are decisions taken here; throttle entities are configured
by the parent orchestrator and addressed by reference, not by spawn.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone


_REPORT_DB_NAME = f"{os.environ.get('ENV_PREFIX', 'dev')}_PublicHealthData"
_REPORT_COLLECTION = "pipeline_discrepancy_reports"
_METRICS_COLLECTION = "pipeline_run_metrics"


def _get_mongo_client():
    from pymongo import MongoClient
    return MongoClient(os.environ["MONGO_connectionString"])


def work_manager_entity_fn(context) -> None:
    state = context.get_state(lambda: {
        "config": None,
        "next_chunk_id": 0,
        "next_worker_id": 1,
        "workers": {},
        "done": 0,
        "discrepancy_count": 0,
        "entity_signal_ops": 0,
        "mongo_write_ops": 0,
    })
    op = context.operation_name
    inp = context.get_input()
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except Exception:
            inp = {}
    if inp is None:
        inp = {}

    state["entity_signal_ops"] = int(state.get("entity_signal_ops", 0)) + 1

    if op == "seed":
        c = {
            "file_size": int(inp["file_size"]),
            "header_end": int(inp["header_end"]),
            "chunk_size_bytes": int(inp.get("chunk_size_bytes", 2_500_000)),
            "batch_size": int(inp.get("batch_size", 500)),
            "discrepancy_threshold": int(inp.get("discrepancy_threshold", 1000)),
        }
        c["total_chunks"] = (
            c["file_size"] - c["header_end"] + c["chunk_size_bytes"] - 1
        ) // c["chunk_size_bytes"]
        state["config"] = c
        context.set_state(state)
        return

    if op == "next_assignment":
        cfg = state["config"]
        if cfg is None:
            context.set_result(None)
            context.set_state(state)
            return
        if state["next_chunk_id"] >= cfg["total_chunks"]:
            context.set_result(None)
            context.set_state(state)
            return
        if state["discrepancy_count"] >= cfg["discrepancy_threshold"]:
            context.set_result(None)
            context.set_state(state)
            return
        cid = state["next_chunk_id"]
        state["next_chunk_id"] = cid + 1
        start = cfg["header_end"] + cid * cfg["chunk_size_bytes"]
        end = min(start + cfg["chunk_size_bytes"], cfg["file_size"])
        worker_id = str(inp["worker_id"])
        state["workers"][worker_id] = {
            "chunk_id": cid,
            "claimed_at": time.time(),
            "kind": state["workers"].get(worker_id, {}).get("kind", "record_worker"),
        }
        context.set_state(state)
        context.set_result({
            "chunk_id": cid,
            "start_byte": start,
            "end_byte": end,
            "batch_size": cfg["batch_size"],
            "assignment_id": cid,
        })
        return

    if op == "ack":
        worker_id = str(inp["worker_id"])
        state["workers"].pop(worker_id, None)
        state["done"] = int(state.get("done", 0)) + 1
        context.set_state(state)
        return

    if op == "spawn_pool":
        pool_size = int(inp["pool_size"])
        spawned = []
        for _ in range(pool_size):
            wid = state["next_worker_id"]
            state["next_worker_id"] = wid + 1
            state["workers"][str(wid)] = {
                "kind": "record_worker",
                "spawned_at": time.time(),
                "age_limit_seconds": int(inp.get("age_limit_seconds", 6600)),
            }
            spawned.append(wid)
        context.set_state(state)
        context.set_result({"spawned_worker_ids": spawned})
        return

    if op == "spawn_source_gather":
        sources = list(inp.get("sources") or [])
        spawned = []
        for src in sources:
            wid = state["next_worker_id"]
            state["next_worker_id"] = wid + 1
            state["workers"][str(wid)] = {
                "kind": f"source_gather:{src}",
                "spawned_at": time.time(),
                "age_limit_seconds": int(inp.get("age_limit_seconds", 6600)),
            }
            spawned.append({"worker_id": wid, "source": src})
        context.set_state(state)
        context.set_result({"spawned": spawned})
        return

    if op == "respawn":
        old_wid = str(inp["worker_id"])
        state["workers"].pop(old_wid, None)
        new_wid = state["next_worker_id"]
        state["next_worker_id"] = new_wid + 1
        state["workers"][str(new_wid)] = {
            "kind": "record_worker",
            "spawned_at": time.time(),
            "age_limit_seconds": int(inp.get("age_limit_seconds", 6600)),
        }
        context.set_state(state)
        context.set_result({"new_worker_id": new_wid})
        return

    if op == "report_discrepancy":
        state["discrepancy_count"] = int(state.get("discrepancy_count", 0)) + 1
        context.set_state(state)
        _persist_discrepancy(inp)
        return

    if op == "record_mongo_writes":
        n = int(inp.get("n", 0))
        state["mongo_write_ops"] = int(state.get("mongo_write_ops", 0)) + n
        context.set_state(state)
        return

    if op == "stats":
        cfg = state["config"]
        context.set_result({
            "configured": cfg is not None,
            "total_chunks": cfg["total_chunks"] if cfg else 0,
            "next_chunk_id": state["next_chunk_id"],
            "next_worker_id": state["next_worker_id"],
            "workers": len(state["workers"]),
            "done": state["done"],
            "discrepancy_count": state["discrepancy_count"],
            "entity_signal_ops": state.get("entity_signal_ops", 0),
            "mongo_write_ops": state.get("mongo_write_ops", 0),
        })
        return

    if op == "emit_metrics":
        load_id = inp.get("load_id", "unknown")
        doc = {
            "load_id": load_id,
            "emitted_at": datetime.now(timezone.utc).isoformat(),
            "kind": "work_manager_summary",
            "entity_signal_ops": state.get("entity_signal_ops", 0),
            "mongo_write_ops": state.get("mongo_write_ops", 0),
            "discrepancy_count": state.get("discrepancy_count", 0),
            "done_assignments": state.get("done", 0),
            "next_chunk_id": state.get("next_chunk_id", 0),
            "next_worker_id": state.get("next_worker_id", 1),
        }
        try:
            _get_mongo_client()[_REPORT_DB_NAME][_METRICS_COLLECTION].insert_one(doc)
        except Exception as exc:
            logging.warning("work_manager: metrics emit failed: %s", exc)
        context.set_state(state)
        return

    if op == "reset":
        state["config"] = None
        state["next_chunk_id"] = 0
        state["next_worker_id"] = 1
        state["workers"] = {}
        state["done"] = 0
        state["discrepancy_count"] = 0
        state["entity_signal_ops"] = 0
        state["mongo_write_ops"] = 0
        context.set_state(state)
        return

    raise ValueError(f"work_manager: unknown operation {op!r}")


def _persist_discrepancy(inp: dict) -> None:
    record_key = inp.get("record_key")
    reason = inp.get("reason", "unknown")
    load_id = inp.get("load_id", "unknown")
    doc = {
        "load_id": load_id,
        "reported_at": datetime.now(timezone.utc).isoformat(),
        "record_key": record_key,
        "reason": reason,
        "context": inp.get("context") or {},
    }
    try:
        _get_mongo_client()[_REPORT_DB_NAME][_REPORT_COLLECTION].insert_one(doc)
    except Exception as exc:
        logging.warning("work_manager: discrepancy persist failed: %s", exc)
