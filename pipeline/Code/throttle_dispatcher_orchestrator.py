from __future__ import annotations

from datetime import timedelta


def throttle_dispatcher_orchestrator_fn(context):
    import azure.durable_functions as df
    cfg = context.get_input() or {}
    throttle_name = cfg["throttle_name"]
    tick_interval_seconds = float(cfg.get("tick_interval_seconds", 0.2))
    max_runtime_seconds = float(cfg.get("max_runtime_seconds", 7200.0))
    throttle_eid = df.EntityId("throttle", throttle_name)
    deadline = context.current_utc_datetime + timedelta(seconds=max_runtime_seconds)
    ticks = 0
    while context.current_utc_datetime < deadline:
        yield context.call_entity(throttle_eid, "tick", {})
        yield context.create_timer(
            context.current_utc_datetime + timedelta(seconds=tick_interval_seconds)
        )
        ticks += 1
    return {"throttle_name": throttle_name, "ticks": ticks, "reason": "deadline_reached"}
