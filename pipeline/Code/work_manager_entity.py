# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""work_manager_entity - the single durable actor for the records-as-messages pipeline.

Owns the per-work-kind assignment queues, the worker roster, the spawn
counter (monotonic, never reused), and the discrepancy counter. Spawns
and respawns are decisions taken here; throttle entities are configured
by the parent orchestrator and addressed by reference, not by spawn.

Two work kinds today:

  cms_chunks      The load phase's CSV byte-range assignments. Seeded by
                  provider_pipeline_orchestrator after build_chunk_index.
                  Each assignment is {chunk_id, start_byte, end_byte,
                  batch_size, work_kind, assignment_id}.

  repair_chunks   The recovery phase's per-NPI assignments. Seeded by
                  provider_pipeline_orchestrator after the load drains,
                  during the recovery phase. Each assignment is
                  {chunk_id, npis: [...], batch_size, work_kind,
                  assignment_id}.

Backward compatibility:
  - Every queue-touching op takes a `work_kind` argument that defaults to
    "cms_chunks". Callers from the existing load path pass nothing and get
    the original behavior bit-for-bit.
  - Existing in-flight entities with top-level pending/claimed/done/config
    get one-time, transparent migration into queues["cms_chunks"] on the
    first op handled after this code lands. The migration is idempotent.
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

_DEFAULT_WORK_KIND = "cms_chunks"
_REPAIR_WORK_KIND = "repair_chunks"
_VALID_WORK_KINDS = (_DEFAULT_WORK_KIND, _REPAIR_WORK_KIND)


def _get_mongo_client():
    from pymongo import MongoClient
    return MongoClient(os.environ["MONGO_connectionString"])


def _migrate_state_to_queues(state: dict) -> None:
    """One-time, transparent promotion of pre-multi-queue state.

    Old shape carried `config`, `pending`, `claimed`, and `done` at the
    top level. New shape moves them under `state["queues"][work_kind]`.
    Idempotent — if `queues` already exists, this is a no-op.
    """
    if "queues" in state:
        return
    queues = {
        _DEFAULT_WORK_KIND: {
            "config": state.pop("config", None),
            "pending": state.pop("pending", []),
            "claimed": state.pop("claimed", {}),
            "done": int(state.pop("done", 0)),
        }
    }
    state["queues"] = queues


def _queue(state: dict, work_kind: str) -> dict:
    """Return the queue dict for `work_kind`, creating an empty one on demand."""
    if work_kind not in _VALID_WORK_KINDS:
        raise ValueError(
            f"work_manager: unknown work_kind {work_kind!r}; "
            f"valid: {_VALID_WORK_KINDS}"
        )
    _migrate_state_to_queues(state)
    queues = state["queues"]
    if work_kind not in queues:
        queues[work_kind] = {"config": None, "pending": [], "claimed": {}, "done": 0}
    return queues[work_kind]


def _all_queues(state: dict) -> dict:
    _migrate_state_to_queues(state)
    return state["queues"]


def work_manager_entity_fn(context) -> None:
    state = context.get_state(lambda: {
        "next_chunk_id": 0,
        "next_worker_id": 1,
        "workers": {},
        "discrepancy_count": 0,
        "entity_signal_ops": 0,
        "mongo_write_ops": 0,
        "queues": {},
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
    work_kind = str(inp.get("work_kind", _DEFAULT_WORK_KIND))

    if op == "seed":
        q = _queue(state, work_kind)
        if work_kind == _DEFAULT_WORK_KIND:
            # chunk_index is required. Precomputed record-boundary byte
            # ranges from build_chunk_index_activity are the only chunking
            # source the load supports. No coarse byte-range fallback.
            chunk_index = inp.get("chunk_index")
            if not chunk_index:
                raise ValueError(
                    "work_manager.seed (cms_chunks): chunk_index is required"
                )
            cfg = {
                "file_size": int(inp["file_size"]),
                "header_end": int(inp["header_end"]),
                "batch_size": int(inp["batch_size"]),
                "discrepancy_threshold": int(inp["discrepancy_threshold"]),
                "total_chunks": len(chunk_index),
            }
            pending: list = []
            for cid, entry in enumerate(chunk_index):
                start, end = entry[0], entry[1]
                pending.append([cid, int(start), int(end)])
            q["config"] = cfg
            q["pending"] = pending
            q["claimed"] = {}
            q["done"] = 0
        elif work_kind == _REPAIR_WORK_KIND:
            # Recovery seed: caller supplies chunk_index where each entry is
            # a dict {stage, npis}. The stage value is the failure source
            # that the assignment's NPIs carry; the worker stamps it onto
            # its metrics for observability and reuses the existing chain
            # logic on the NPIs themselves (no per-stage dispatch).
            chunk_index = inp.get("chunk_index") or []
            cfg = {
                "batch_size": int(inp.get("batch_size", 200)),
                "discrepancy_threshold": int(inp.get("discrepancy_threshold", 1000)),
                "total_chunks": len(chunk_index),
            }
            pending = []
            for cid, entry in enumerate(chunk_index):
                stage = entry.get("stage") if isinstance(entry, dict) else None
                npis = entry.get("npis") if isinstance(entry, dict) else entry
                pending.append([cid, stage, list(npis or [])])
            q["config"] = cfg
            q["pending"] = pending
            q["claimed"] = {}
            q["done"] = 0
        context.set_state(state)
        return

    if op == "next_assignment":
        q = _queue(state, work_kind)
        cfg = q.get("config")
        if cfg is None:
            context.set_result(None)
            context.set_state(state)
            return
        if state["discrepancy_count"] >= int(cfg.get("discrepancy_threshold", 1000)):
            context.set_result(None)
            context.set_state(state)
            return
        pending = q.get("pending") or []
        if not pending:
            context.set_result(None)
            context.set_state(state)
            return
        worker_id = str(inp["worker_id"])
        entry = pending.pop(0)
        now = time.time()
        cid = entry[0]
        if work_kind == _DEFAULT_WORK_KIND:
            start, end = entry[1], entry[2]
            assignment = {
                "work_kind": work_kind,
                "chunk_id": cid,
                "assignment_id": cid,
                "start_byte": start,
                "end_byte": end,
                "batch_size": cfg["batch_size"],
            }
        else:  # repair_chunks
            stage = entry[1]
            npis = entry[2]
            assignment = {
                "work_kind": work_kind,
                "chunk_id": cid,
                "assignment_id": cid,
                "stage": stage,
                "npis": npis,
                "batch_size": cfg["batch_size"],
            }
        q["claimed"][str(cid)] = {
            "worker_id": worker_id,
            "claimed_at": now,
            "work_kind": work_kind,
            "entry": entry,
        }
        state["workers"][worker_id] = {
            "chunk_id": cid,
            "claimed_at": now,
            "work_kind": work_kind,
            "kind": state["workers"].get(worker_id, {}).get("kind", "record_worker"),
        }
        q["pending"] = pending
        state["next_chunk_id"] = max(int(state.get("next_chunk_id", 0)), cid + 1)
        context.set_state(state)
        context.set_result(assignment)
        return

    if op == "next_assignment_batch":
        q = _queue(state, work_kind)
        cfg = q.get("config")
        if cfg is None:
            context.set_result([])
            context.set_state(state)
            return
        if state["discrepancy_count"] >= int(cfg.get("discrepancy_threshold", 1000)):
            context.set_result([])
            context.set_state(state)
            return
        pending = q.get("pending") or []
        n = max(1, int(inp.get("n", 5)))
        worker_id = str(inp["worker_id"])
        out: list = []
        now = time.time()
        for _ in range(n):
            if not pending:
                break
            entry = pending.pop(0)
            cid = entry[0]
            q["claimed"][str(cid)] = {
                "worker_id": worker_id, "claimed_at": now, "work_kind": work_kind,
            }
            if work_kind == _DEFAULT_WORK_KIND:
                out.append({
                    "work_kind": work_kind,
                    "chunk_id": cid,
                    "assignment_id": cid,
                    "start_byte": entry[1],
                    "end_byte": entry[2],
                    "batch_size": cfg["batch_size"],
                })
            else:
                out.append({
                    "work_kind": work_kind,
                    "chunk_id": cid,
                    "assignment_id": cid,
                    "stage": entry[1],
                    "npis": entry[2],
                    "batch_size": cfg["batch_size"],
                })
        if out:
            state["workers"][worker_id] = {
                "chunk_ids": [a["chunk_id"] for a in out],
                "claimed_at": now,
                "work_kind": work_kind,
                "kind": state["workers"].get(worker_id, {}).get("kind", "record_worker"),
            }
            state["next_chunk_id"] = max(int(state.get("next_chunk_id", 0)), out[-1]["chunk_id"] + 1)
        q["pending"] = pending
        context.set_state(state)
        context.set_result(out)
        return

    if op == "ack":
        q = _queue(state, work_kind)
        worker_id = str(inp["worker_id"])
        chunk_id = inp.get("chunk_id")
        if chunk_id is not None:
            q["claimed"].pop(str(chunk_id), None)
        else:
            for cid_str, claim in list(q.get("claimed", {}).items()):
                if claim.get("worker_id") == worker_id:
                    q["claimed"].pop(cid_str, None)
        w = state["workers"].get(worker_id)
        if w and chunk_id is not None and "chunk_ids" in w:
            try:
                w["chunk_ids"].remove(int(chunk_id))
            except ValueError:
                pass
            if not w["chunk_ids"]:
                state["workers"].pop(worker_id, None)
        else:
            state["workers"].pop(worker_id, None)
        q["done"] = int(q.get("done", 0)) + 1
        context.set_state(state)
        return

    if op == "release":
        # A worker calls release when its activity failed. Move the chunk
        # from claimed BACK ONTO pending so a sibling worker (or a fresh
        # replay after a process restart) picks it up. Without release,
        # any failed activity would orphan its chunk in claimed and the
        # run would be silently short by exactly the failed chunks.
        # Per Skip 2026-05-29: assignment reassignment is a requirement,
        # not a fallback.
        q = _queue(state, work_kind)
        chunk_id = str(inp["chunk_id"])
        claim = q.get("claimed", {}).pop(chunk_id, None)
        if claim is not None and "entry" in claim:
            q.setdefault("pending", []).insert(0, claim["entry"])
        state["workers"].pop(str(inp.get("worker_id", "")), None)
        context.set_state(state)
        return

    if op == "ack_batch":
        q = _queue(state, work_kind)
        worker_id = str(inp["worker_id"])
        chunk_ids = inp.get("chunk_ids") or []
        for cid in chunk_ids:
            q["claimed"].pop(str(cid), None)
        state["workers"].pop(worker_id, None)
        q["done"] = int(q.get("done", 0)) + len(chunk_ids)
        context.set_state(state)
        return

    if op == "reset_stale":
        # Reaper: chunks claimed > max_age_seconds ago are dropped from the
        # claimed set. Reclaim does NOT reconstruct the assignment — neither
        # queue's pending entries are reconstructable from cfg alone (cms
        # chunks come from a precomputed chunk_index, repair chunks carry
        # NPI lists). The next pipeline run rebuilds the cohort from source
        # state on either side.
        q = _queue(state, work_kind)
        max_age = float(inp.get("max_age_seconds", 1800))
        now = time.time()
        reclaimed: list = []
        for cid_str, claim in list(q.get("claimed", {}).items()):
            if now - float(claim.get("claimed_at", now)) > max_age:
                q["claimed"].pop(cid_str, None)
                reclaimed.append(int(cid_str))
        context.set_state(state)
        context.set_result({"reclaimed": reclaimed, "work_kind": work_kind})
        return

    if op == "spawn_pool":
        pool_size = int(inp["pool_size"])
        spawned: list = []
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
        queues = _all_queues(state)
        per_queue = {}
        for kind, q in queues.items():
            cfg = q.get("config") or {}
            per_queue[kind] = {
                "configured": q.get("config") is not None,
                "total_chunks": int(cfg.get("total_chunks", 0)),
                "pending": len(q.get("pending") or []),
                "claimed": len(q.get("claimed") or {}),
                "done": int(q.get("done", 0)),
            }
        context.set_result({
            "queues": per_queue,
            "next_chunk_id": state["next_chunk_id"],
            "next_worker_id": state["next_worker_id"],
            "workers": len(state["workers"]),
            "discrepancy_count": state["discrepancy_count"],
            "entity_signal_ops": state.get("entity_signal_ops", 0),
            "mongo_write_ops": state.get("mongo_write_ops", 0),
        })
        return

    if op == "emit_metrics":
        queues = _all_queues(state)
        load_id = inp.get("load_id", "unknown")
        doc = {
            "load_id": load_id,
            "emitted_at": datetime.now(timezone.utc).isoformat(),
            "kind": "work_manager_summary",
            "entity_signal_ops": state.get("entity_signal_ops", 0),
            "mongo_write_ops": state.get("mongo_write_ops", 0),
            "discrepancy_count": state.get("discrepancy_count", 0),
            "next_chunk_id": state.get("next_chunk_id", 0),
            "next_worker_id": state.get("next_worker_id", 1),
            "queues": {
                kind: {
                    "done": int(q.get("done", 0)),
                    "total_chunks": int((q.get("config") or {}).get("total_chunks", 0)),
                }
                for kind, q in queues.items()
            },
        }
        try:
            _get_mongo_client()[_REPORT_DB_NAME][_METRICS_COLLECTION].insert_one(doc)
        except Exception as exc:
            logging.warning("work_manager: metrics emit failed: %s", exc)
        context.set_state(state)
        return

    if op == "reset":
        state["next_chunk_id"] = 0
        state["next_worker_id"] = 1
        state["workers"] = {}
        state["discrepancy_count"] = 0
        state["entity_signal_ops"] = 0
        state["mongo_write_ops"] = 0
        state["queues"] = {}
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
