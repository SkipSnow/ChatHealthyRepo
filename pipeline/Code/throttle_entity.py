# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""throttle_entity - generic token-bucket Durable Entity.

Canonical Microsoft-supported pattern: orchestrators call this entity via
context.call_entity (two-way). request_permission computes refill on demand
and returns immediately with either {"granted": True} or {"granted": False,
"retry_after_seconds": N}. Callers poll with create_timer between attempts.

No waiters list. No callback queues. No HTTP-management-API signaling. No
storage queue. The entity holds bucket state and answers one question:
"are there n tokens free right now, and if not, how long until there are?"

Operations:
  configure(refill_rate, capacity)   - set rate and capacity; fill bucket
  request_permission(n)               - deduct n if available; else compute
                                        deficit / refill_rate as retry_after
  peek                                - diagnostics: tokens, counters
  reset                               - clean-environment hygiene
"""
from __future__ import annotations
from chathealthy_frontend_lib.exceptions import ChatHealthyException

import json
import time


def throttle_entity_fn(context) -> None:
    state = context.get_state(lambda: {
        "refill_rate": None,
        "capacity": None,
        "tokens": 0.0,
        "last_refill_at_epoch": time.time(),
        "requests_received": 0,
        "grants_issued": 0,
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

    if op == "configure":
        if not isinstance(inp, dict):
            raise ChatHealthyException(mode="value_error", message=f"throttle.configure requires dict, got {inp!r}")
        rate = inp.get("refill_rate")
        cap = inp.get("capacity")
        if rate is None or cap is None:
            raise ChatHealthyException(mode="value_error", message=f"throttle.configure requires refill_rate AND capacity, got {inp!r}")
        state["refill_rate"] = float(rate)
        state["capacity"] = int(cap)
        state["tokens"] = float(cap)
        state["last_refill_at_epoch"] = time.time()
        state["requests_received"] = 0
        state["grants_issued"] = 0
        context.set_state(state)
        context.set_result({
            "configured": True,
            "refill_rate": state["refill_rate"],
            "capacity": state["capacity"],
        })
        return

    if op == "reset":
        state.update({
            "refill_rate": None,
            "capacity": None,
            "tokens": 0.0,
            "last_refill_at_epoch": time.time(),
            "requests_received": 0,
            "grants_issued": 0,
        })
        context.set_state(state)
        context.set_result({"reset": True})
        return

    if state.get("refill_rate") is None:
        if op == "peek":
            context.set_result({"configured": False})
            return
        raise ChatHealthyException(mode="value_error", message=f"throttle: entity not configured, cannot {op}")

    # Refill on demand using wall-clock epoch seconds. time.monotonic() is
    # process-local and unsafe across entity activations on different hosts.
    now = time.time()
    elapsed = max(0.0, now - float(state.get("last_refill_at_epoch", now)))
    state["tokens"] = min(
        float(state["capacity"]),
        float(state["tokens"]) + elapsed * float(state["refill_rate"]),
    )
    state["last_refill_at_epoch"] = now

    if op == "request_permission":
        n = int(inp.get("n", 1))
        if n < 1:
            raise ChatHealthyException(mode="value_error", message=f"throttle.request_permission: n must be >=1, got {n}")
        if n > int(state["capacity"]):
            raise ChatHealthyException(mode="value_error", message=f"throttle.request_permission: n={n} exceeds capacity={state['capacity']}; "
                f"caller must split the request")
        state["requests_received"] = int(state.get("requests_received", 0)) + 1
        if state["tokens"] >= n:
            state["tokens"] -= n
            state["grants_issued"] = int(state.get("grants_issued", 0)) + 1
            context.set_state(state)
            context.set_result({"granted": True, "tokens_remaining": state["tokens"]})
            return
        deficit = float(n) - float(state["tokens"])
        rate = float(state["refill_rate"])
        retry_after = (deficit / rate) if rate > 0 else 1.0
        # Floor at 0.05s to prevent thrash; cap at 60s to bound orchestrator wait history.
        retry_after = max(0.05, min(retry_after, 60.0))
        context.set_state(state)
        context.set_result({
            "granted": False,
            "retry_after_seconds": retry_after,
            "tokens_available": state["tokens"],
        })
        return

    if op == "peek":
        context.set_state(state)
        context.set_result({
            "configured": True,
            "refill_rate": state["refill_rate"],
            "capacity": state["capacity"],
            "tokens": state["tokens"],
            "requests_received": int(state.get("requests_received", 0)),
            "grants_issued": int(state.get("grants_issued", 0)),
        })
        return

    raise ChatHealthyException(mode="value_error", message=f"throttle: unknown operation {op!r}")
