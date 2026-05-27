# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""record_worker_orchestrator - per-pool-slot sub-orchestration.

Loop: ask work manager for next assignment, acquire one pool_size permit
(chunk-admission control), run process_assignment_activity, ack, repeat
until queue drained or age limit reached.

Per-API rate limiting (Census/Maps/NPPES/OpenAI) happens inside the activity
via in-process _TokenBucket instances. The Durable @throttle@ surface is
intentionally minimal: pool_size for concurrency and source_gather for the
one-shot startup gate.
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
        assignment = yield context.call_entity(wm, "next_assignment", {"worker_id": wid})
        if assignment is None:
            break

        yield from acquire(context, "pool_size", n=1)

        yield context.call_activity("process_assignment_activity", {
            **cfg,
            "assignment": assignment,
        })
        yield context.call_entity(wm, "ack", {"worker_id": wid})

        elapsed = (context.current_utc_datetime - start_time).total_seconds()
        if elapsed > age_limit:
            yield context.call_entity(wm, "respawn", {"worker_id": wid})
            break
    return {"worker_id": wid, "reason": "drained_or_aged_out"}
