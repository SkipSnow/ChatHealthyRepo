# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""recovery_worker_orchestrator - per-pool-slot recovery sub-orchestration.

Mirror of record_worker_orchestrator but pulls assignments off the
work_manager entity's repair_chunks queue (work_kind="repair_chunks").
Each iteration claims ONE NPI-list assignment, hands it to
process_recovery_assignment_activity, then acks. The pool_size throttle
is shared with the load phase — recovery either runs after load drains
(provider_pipeline_orchestrator_fn fans both in sequence) or with
whatever pool_size capacity remains.

Per-API rate limits are enforced inside the activity via the shared
_TokenBucket instances in process_assignment_activity.
"""
from __future__ import annotations


def recovery_worker_orchestrator_fn(context):
    import azure.durable_functions as df
    from throttle_acquire import acquire

    cfg = context.get_input() or {}
    wid = cfg["worker_id"]
    load_id = cfg["load_id"]
    age_limit = int(cfg.get("age_limit_seconds", 6600))
    work_kind = "repair_chunks"

    wm = df.EntityId("work_manager", load_id)
    start_time = context.current_utc_datetime

    while True:
        assignment = yield context.call_entity(wm, "next_assignment", {
            "worker_id": wid, "work_kind": work_kind,
        })
        if not assignment:
            break

        yield from acquire(context, "pool_size", n=1)

        yield context.call_activity("process_recovery_assignment_activity", {
            **cfg,
            "assignment": assignment,
        })

        yield context.call_entity(wm, "ack", {
            "worker_id": wid,
            "chunk_id": assignment.get("chunk_id"),
            "work_kind": work_kind,
        })

        elapsed = (context.current_utc_datetime - start_time).total_seconds()
        if elapsed > age_limit:
            yield context.call_entity(wm, "respawn", {"worker_id": wid})
            break
    return {"worker_id": wid, "reason": "drained_or_aged_out", "work_kind": work_kind}
