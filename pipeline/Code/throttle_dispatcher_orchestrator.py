from __future__ import annotations

from datetime import timedelta


def throttle_dispatcher_orchestrator_fn(context):
    import azure.durable_functions as df
    cfg = context.get_input() or {}
    throttle_name = cfg["throttle_name"]
    tick_interval_seconds = float(cfg.get("tick_interval_seconds", 0.2))
    max_ticks = int(cfg.get("max_ticks", 360_000))
    throttle_eid = df.EntityId("throttle", throttle_name)
    for _ in range(max_ticks):
        if context.get_input() is None:
            break
        yield context.call_entity(throttle_eid, "tick", {})
        yield context.create_timer(
            context.current_utc_datetime + timedelta(seconds=tick_interval_seconds)
        )
    return {"throttle_name": throttle_name, "reason": "max_ticks_reached"}
