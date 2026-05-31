# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""record_worker_orchestrator - per-pool-slot sub-orchestration.

Per Skip 2026-05-27: each iteration claims ONE assignment, runs the
activity for that one assignment, then acks that one assignment. No
batched reservations. Workers never hold more chunks than they're
about to process. ack moves per chunk so stalled work surfaces fast
instead of being hidden behind a slowest-of-N batch.

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

    wm = df.EntityId("work_manager", load_id)
    start_time = context.current_utc_datetime

    while True:
        # Claim ONE assignment per iteration. No reservation stash.
        assignment = yield context.call_entity(wm, "next_assignment", {
            "worker_id": wid,
        })
        if not assignment:
            break

        # One pool_size token per chunk. Throttle name is per-run to avoid
        # tombstone collisions when cleanup terminates+purges the entity.
        yield from acquire(context, f"pool_size@{load_id}", n=1)

        try:
            yield context.call_activity("process_assignment_activity", {
                **cfg,
                "assignment": assignment,
            })
            # Ack this one chunk so done/claimed move per chunk.
            yield context.call_entity(wm, "ack", {
                "worker_id": wid, "chunk_id": assignment.get("chunk_id"),
            })
        except Exception:
            # The activity failed (OOM, transient Mongo, anything). Per
            # Skip 2026-05-29 the assignment MUST be reassigned, not
            # silently dropped. Release moves it from claimed back to
            # pending so a sibling worker (or this worker on the next
            # loop iteration) claims it. Continue the loop — the
            # orchestration stays alive and the run progresses.
            yield context.call_entity(wm, "release", {
                "worker_id": wid, "chunk_id": assignment.get("chunk_id"),
            })

        elapsed = (context.current_utc_datetime - start_time).total_seconds()
        if elapsed > age_limit:
            yield context.call_entity(wm, "respawn", {"worker_id": wid})
            break
    return {"worker_id": wid, "reason": "drained_or_aged_out"}
