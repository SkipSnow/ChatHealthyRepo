# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""task_manager — Durable Entity work-allocator for the streaming pipeline.

Records-as-messages architecture (Skip 2026-05-26):

    The partition (worker) goes back and sends a message to the parser
    to get 5K more assignments.

The "parser" here is `task_manager_entity` — a single-threaded actor that
owns the queue of byte-offset chunks. Workers (activities) call it
continuously: claim → process → release → claim → ... → exit when queue
is empty. Continuous flow, no orchestrator fan-out waves with 5-10 min
gaps between batches.

Topology
--------
    streaming_pipeline_orchestrator
        ├── (1) signals task_manager.seed(file_size, header_end, chunk_size_bytes)
        └── (2) spawns N stateless streaming_partition_activity workers
                ├── each worker LOOPS:
                │     a. POST signal_entity('claim', {worker_id, request_id})
                │     b. poll GET entity state for pending_responses[request_id]
                │     c. process chunk inline (parse + normalize + pass1 + bulk-write)
                │     d. POST signal_entity('release', {chunk_id})
                │     e. if claim returned None → exit; else loop
                └── returns when its queue is drained

Chunk sizing
------------
Skip's directive: ~5,000 records per chunk. NPPES rows average ~500 bytes,
so chunk_size_bytes ≈ 2.5 MB. The entity seeds chunks by walking
[header_end, file_size] in 2.5 MB strides aligned to newlines. This stays
well under the 2 h Durable activity timeout: at 15 rec/sec/worker × 5000
records = 333 sec = 5.6 min per chunk.

Throughput math (national NPPES, 8.8 M records):
    8.8 M / 5 K = 1,760 chunks
    1,760 / 100 workers = 17.6 chunks per worker
    17.6 × 5.6 min = ~98 min per worker  ← under 105 min total target

Auth contract
-------------
Activities call the entity via the Durable Functions HTTP management API.
This requires `DURABLE_MGMT_CODE` (FA master key) as an app setting. The
helper raises immediately if it's missing — fail-loud, no fallback.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

_log = logging.getLogger("task_manager")

# Polling cadence for claim responses. 200 ms is fast enough that worker
# idle-time is small (each claim adds 0-200 ms of polling latency) and
# slow enough that we don't hammer the entity state endpoint.
_POLL_INTERVAL_SECONDS = 0.2
_CLAIM_MAX_WAIT_SECONDS = 30.0  # claim should respond within 30 s; else error


# ── Durable Entity body ─────────────────────────────────────────────────────

def task_manager_entity_fn(context) -> None:
    """Single-threaded actor body. Wired in function_app.py:

        @app.entity_trigger(context_name="context")
        def task_manager(context: df.DurableEntityContext) -> None:
            task_manager_entity_fn(context)

    State shape (durable):
        {
            "config": {"file_size": int, "header_end": int, "chunk_size_bytes": int} | None,
            "queue":  [ {chunk_id, start_byte, end_byte}, ... ],   # FIFO
            "in_flight": { chunk_id: {worker_id, claimed_at}, ... },
            "done":      int,                # completion counter (don't keep payloads in state)
            "pending_responses": { request_id: chunk_or_None, ... },  # claim results awaiting worker pickup
        }

    Operations:
        seed({"file_size": int, "header_end": int, "chunk_size_bytes": int=2_500_000})
            Populate queue. Idempotent on identical args; raises on re-seed
            with different args (configuration drift = fail loud).

        claim({"worker_id": str, "request_id": str})
            Atomic pop from queue. Stores result in pending_responses[request_id].
            Worker polls state for its request_id, then sends 'ack' to clear it.
            Result is the chunk dict or None when queue is empty.

        ack({"request_id": str})
            Remove pending_responses[request_id] after worker has read it.

        release({"chunk_id": int})
            Remove chunk from in_flight, increment done counter.

        stats()
            Return {queued, in_flight, done, configured}.
    """
    state = context.get_state(lambda: {
        "config":            None,
        "queue":             [],
        "in_flight":         {},
        "done":              0,
        "pending_responses": {},
    })
    op_name = context.operation_name
    op_input = context.get_input() or {}

    if op_name == "seed":
        file_size        = int(op_input["file_size"])
        header_end       = int(op_input["header_end"])
        chunk_size_bytes = int(op_input.get("chunk_size_bytes", 2_500_000))

        # Idempotent re-seed when config matches; loud failure otherwise.
        if state["config"] is not None:
            existing = state["config"]
            if (existing["file_size"] == file_size and
                existing["header_end"] == header_end and
                existing["chunk_size_bytes"] == chunk_size_bytes):
                context.set_result({"chunks": len(state["queue"]) + len(state["in_flight"]) + state["done"],
                                    "already_seeded": True})
                return
            raise ValueError(
                f"task_manager already seeded with different config "
                f"(existing={existing}, new={op_input}); reset state before re-seeding"
            )

        # Build the chunk queue. Each chunk is [cur, min(cur+chunk_size, file_size)).
        # NOTE: byte boundaries are NOT pre-aligned to \n here — the worker's
        # CSV streamer drops partial leading rows defensively (same pattern as
        # the legacy provider_worker).
        chunks = []
        cur = header_end
        cid = 0
        while cur < file_size:
            nxt = min(cur + chunk_size_bytes, file_size)
            chunks.append({"chunk_id": cid, "start_byte": cur, "end_byte": nxt})
            cid += 1
            cur = nxt
        state["queue"] = chunks
        state["config"] = {
            "file_size":        file_size,
            "header_end":       header_end,
            "chunk_size_bytes": chunk_size_bytes,
        }
        context.set_state(state)
        context.set_result({"chunks": len(chunks), "already_seeded": False})
        return

    if op_name == "claim":
        worker_id  = op_input["worker_id"]
        request_id = op_input["request_id"]
        if state["queue"]:
            chunk = state["queue"].pop(0)
            state["in_flight"][str(chunk["chunk_id"])] = {
                "worker_id":  worker_id,
                "claimed_at": time.time(),
            }
            state["pending_responses"][request_id] = chunk
        else:
            state["pending_responses"][request_id] = None  # signal queue empty
        context.set_state(state)
        return

    if op_name == "ack":
        request_id = op_input["request_id"]
        state["pending_responses"].pop(request_id, None)
        context.set_state(state)
        return

    if op_name == "release":
        chunk_id = str(op_input["chunk_id"])
        if chunk_id in state["in_flight"]:
            state["in_flight"].pop(chunk_id)
            state["done"] += 1
        context.set_state(state)
        return

    if op_name == "stats":
        context.set_result({
            "configured": state["config"] is not None,
            "queued":     len(state["queue"]),
            "in_flight":  len(state["in_flight"]),
            "done":       state["done"],
            "config":     state["config"],
        })
        return

    if op_name == "reset":
        # For test/dev only. Production runs do not call reset; the entity
        # is implicitly fresh per-pipeline-run because seed() raises on
        # config drift.
        state["config"] = None
        state["queue"] = []
        state["in_flight"] = {}
        state["done"] = 0
        state["pending_responses"] = {}
        context.set_state(state)
        return

    raise ValueError(f"task_manager: unknown operation {op_name!r}")


# ── Activity-side HTTP helpers ──────────────────────────────────────────────

_ENTITY_NAME = "task_manager"
_ENTITY_KEY  = "default"   # single instance per pipeline run; key chosen by orchestrator


def _entity_base_url() -> str:
    """Build the Durable Functions HTTP management API URL for our entity."""
    host = os.environ.get("WEBSITE_HOSTNAME")
    code = os.environ.get("DURABLE_MGMT_CODE")
    task_hub = os.environ.get("DURABLE_TASK_HUB") or "DevPipelineNetherite2"
    connection = os.environ.get("DURABLE_TASK_CONNECTION") or "AzureWebJobsStorage"
    if not host:
        raise RuntimeError(
            "task_manager helper requires WEBSITE_HOSTNAME env var (Azure Functions sets this)"
        )
    if not code:
        raise RuntimeError(
            "task_manager helper requires DURABLE_MGMT_CODE app setting "
            "(the FA master key). Set it via "
            "`az functionapp config appsettings set --name <fa> --resource-group <rg> "
            "--settings DURABLE_MGMT_CODE=<masterKey>`"
        )
    base = (
        f"https://{host}/runtime/webhooks/durabletask/entities/"
        f"{_ENTITY_NAME}/{_ENTITY_KEY}"
        f"?taskHub={urllib.parse.quote(task_hub)}"
        f"&connection={urllib.parse.quote(connection)}"
        f"&code={urllib.parse.quote(code)}"
    )
    return base


def _signal(op: str, payload: dict | None = None) -> None:
    """One-way signal to the task_manager entity."""
    url = _entity_base_url() + f"&op={urllib.parse.quote(op)}"
    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        _ = r.read()


def _read_state() -> dict:
    """Read current entity state via GET. Returns the raw state dict."""
    url = _entity_base_url()
    with urllib.request.urlopen(url, timeout=20) as r:
        body = r.read().decode("utf-8")
    # The entity GET returns {"state": <state_dict>} or the state directly
    # depending on runtime version. Handle both.
    parsed = json.loads(body)
    if isinstance(parsed, dict) and "state" in parsed:
        return parsed["state"] or {}
    return parsed or {}


def claim_next_chunk(worker_id: str) -> dict | None:
    """Atomically claim the next chunk for `worker_id`.

    Returns the chunk dict {chunk_id, start_byte, end_byte} or None if the
    queue is empty (signaling the worker to exit its loop).

    Raises TimeoutError if the entity doesn't respond within
    _CLAIM_MAX_WAIT_SECONDS (signals a hub-level failure — the orchestration
    should fail fast rather than spin).
    """
    request_id = uuid.uuid4().hex
    _signal("claim", {"worker_id": worker_id, "request_id": request_id})

    deadline = time.monotonic() + _CLAIM_MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)
        try:
            state = _read_state()
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            _log.warning("task_manager state poll failed: %s", exc)
            continue
        pending = (state or {}).get("pending_responses") or {}
        if request_id in pending:
            chunk = pending[request_id]
            # Ack the entity to clear our response from state
            try:
                _signal("ack", {"request_id": request_id})
            except Exception as exc:
                _log.warning("task_manager ack failed (non-fatal): %s", exc)
            return chunk  # may be None when queue is empty

    raise TimeoutError(
        f"task_manager.claim did not respond within {_CLAIM_MAX_WAIT_SECONDS}s "
        f"for worker_id={worker_id!r}"
    )


def release_chunk(chunk_id: int) -> None:
    """Tell the task manager the chunk is fully processed (in Mongo). Idempotent."""
    _signal("release", {"chunk_id": chunk_id})
