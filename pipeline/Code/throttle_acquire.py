# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""throttle_acquire - orchestrator-side helper for the @throttle entity.

Microsoft documents two-way `call_entity` from an orchestrator as the
canonical entity access path; activities are not a supported context
(https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-entities).

acquire(context, throttle_name, n) polls the throttle entity for n tokens,
backing off with create_timer using the entity's reported retry_after_seconds.
Replay-safe: relies only on context.call_entity, context.create_timer, and
context.current_utc_datetime, all of which the Python Durable Functions API
documents as deterministic.
"""
from __future__ import annotations

from datetime import timedelta


_DEFAULT_TIMEOUT_SECONDS = 600
_FLOOR_BACKOFF_SECONDS = 0.05


def acquire(context, throttle_name: str, n: int = 1, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS):
    """Generator: yield until n tokens are granted by @throttle@{throttle_name}.

    Use with `yield from acquire(context, "census", n=...)` inside an
    orchestrator. The entity returns {"granted": True} on success or
    {"granted": False, "retry_after_seconds": N} on token shortfall.
    """
    import azure.durable_functions as df

    throttle_id = df.EntityId("throttle", throttle_name)
    deadline = context.current_utc_datetime + timedelta(seconds=float(timeout_seconds))
    while context.current_utc_datetime < deadline:
        result = yield context.call_entity(throttle_id, "request_permission", {"n": int(n)})
        if isinstance(result, dict) and result.get("granted"):
            return
        wait_s = _FLOOR_BACKOFF_SECONDS
        if isinstance(result, dict):
            wait_s = float(result.get("retry_after_seconds", _FLOOR_BACKOFF_SECONDS))
        wait_s = max(_FLOOR_BACKOFF_SECONDS, wait_s)
        yield context.create_timer(
            context.current_utc_datetime + timedelta(seconds=wait_s)
        )
    raise TimeoutError(
        f"throttle.{throttle_name}: not granted n={n} within {timeout_seconds}s"
    )
