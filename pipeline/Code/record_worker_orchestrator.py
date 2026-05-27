# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""record_worker_orchestrator - per-pool-slot sub-orchestration.

Loop: ask work manager for next assignment, acquire throttle permits from
each external-API entity for the chunk's worst-case usage, run
process_assignment_activity, ack, repeat until queue drained or age limit
reached.

Throttle acquisition lives in the orchestrator (Microsoft-supported
call_entity pattern). The activity runs with permits already granted and
makes API calls freely within budget.
"""
from __future__ import annotations


def record_worker_orchestrator_fn(context):
    import azure.durable_functions as df
    from throttle_acquire import acquire

    cfg = context.get_input() or {}
    wid = cfg["worker_id"]
    load_id = cfg["load_id"]
    age_limit = int(cfg.get("age_limit_seconds", 6600))
    batch_size = int(cfg.get("batch_size", 500))

    # Worst-case per-record API calls. Used to size each pre-chunk acquire.
    # These factors must be <= entity capacities configured by the parent
    # orchestrator's throttle_defaults; the entity raises if n > capacity.
    census_per_record = int(cfg.get("census_per_record", 8))     # 6 practice + billing + slack
    maps_per_record = int(cfg.get("maps_per_record", 3))
    nppes_per_record = int(cfg.get("nppes_per_record", 1))
    openai_per_record = int(cfg.get("openai_per_record", 1))

    wm = df.EntityId("work_manager", load_id)
    start_time = context.current_utc_datetime

    while True:
        assignment = yield context.call_entity(wm, "next_assignment", {"worker_id": wid})
        if assignment is None:
            break

        yield from acquire(context, "census",      n=batch_size * census_per_record)
        yield from acquire(context, "google_maps", n=batch_size * maps_per_record)
        yield from acquire(context, "nppes",       n=batch_size * nppes_per_record)
        yield from acquire(context, "openai",      n=batch_size * openai_per_record)

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
