# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""record_worker_orchestrator - per-pool-slot sub-orchestration.

Pulls a batch of assignments from work_manager (in-RAM deque), claims one
pool_size token for the batch, runs process_assignment_activity per chunk,
sends fire-and-forget acks. Cuts the per-chunk blocking entity ops from 3
to ~0.4.

Per-API rate limiting (Census/Maps/NPPES/OpenAI) happens inside the activity
via in-process _TokenBucket. The Durable @throttle@ surface is intentionally
minimal: pool_size for concurrency, source_gather for startup gate.
"""
from __future__ import annotations


def record_worker_orchestrator_fn(context):
    import azure.durable_functions as df
    from throttle_acquire import acquire

    cfg = context.get_input() or {}
    wid = cfg["worker_id"]
    load_id = cfg["load_id"]
    age_limit = int(cfg.get("age_limit_seconds", 6600))
    claim_n = int(cfg.get("claim_batch", 5))

    wm = df.EntityId("work_manager", load_id)
    start_time = context.current_utc_datetime

    while True:
        # Batch-claim N assignments in one entity call instead of N calls.
        batch = yield context.call_entity(wm, "next_assignment_batch", {
            "worker_id": wid, "n": claim_n,
        })
        if not batch:
            break

        # One pool_size token for the whole batch.
        yield from acquire(context, "pool_size", n=1)

        completed_chunk_ids = []
        for assignment in batch:
            yield context.call_activity("process_assignment_activity", {
                **cfg,
                "assignment": assignment,
            })
            completed_chunk_ids.append(assignment.get("chunk_id"))

        # One batched ack per batch (deterministic + replay-safe). Awaiting
        # is cheap — work_manager.ack_batch is an O(N) state update.
        if completed_chunk_ids:
            yield context.call_entity(wm, "ack_batch", {
                "worker_id": wid, "chunk_ids": completed_chunk_ids,
            })

        elapsed = (context.current_utc_datetime - start_time).total_seconds()
        if elapsed > age_limit:
            yield context.call_entity(wm, "respawn", {"worker_id": wid})
            break
    return {"worker_id": wid, "reason": "drained_or_aged_out"}
