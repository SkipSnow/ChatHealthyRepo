# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""throttle_entity - generic token-bucket Durable Entity with waiters and callbacks.

State: {refill_rate, capacity, tokens, last_refill_at, waiters[]}.

Operations:
  configure   - set rate, capacity; fill bucket
  request_permission(n, request_id, callback_instance_id) - either deduct n tokens
        immediately and deliver a {granted: true} callback, or append to waiters[]
        (cap = pool_size; overflow is rejected loud).
  tick        - refill, scan waiters[] FIFO, grant any whose n fits, deliver callbacks.
  peek        - diagnostics.

Callback delivery: posts a message to a Storage Queue named
'throttle-callbacks-{callback_instance_id}' that the activity polls via
throttle_callback.py.
"""
from __future__ import annotations

import json
import logging
import os
import time


_DEFAULT_WAITER_CAP = 200


def _enqueue_grant(callback_instance_id: str, request_id: str, n: int) -> None:
    try:
        from azure.storage.queue import QueueClient
    except Exception as exc:
        logging.error("throttle_entity: azure.storage.queue unavailable: %s", exc)
        return
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or os.environ.get("AzureWebJobsStorage")
    if not conn:
        logging.error("throttle_entity: storage connection string missing for callback")
        return
    queue_name = _safe_queue_name(f"throttle-callbacks-{callback_instance_id}")
    try:
        client = QueueClient.from_connection_string(conn, queue_name)
        try:
            client.create_queue()
        except Exception:
            pass
        client.send_message(json.dumps({"request_id": request_id, "granted": True, "n": n}))
    except Exception as exc:
        logging.error("throttle_entity: queue send failed: %s", exc)


def _safe_queue_name(raw: str) -> str:
    out_chars = []
    for ch in raw.lower():
        if ch.isalnum() or ch == "-":
            out_chars.append(ch)
        else:
            out_chars.append("-")
    name = "".join(out_chars).strip("-")
    while "--" in name:
        name = name.replace("--", "-")
    if len(name) < 3:
        name = (name + "-throttle")[:63]
    return name[:63]


def throttle_entity_fn(context) -> None:
    state = context.get_state(lambda: {
        "refill_rate": None,
        "capacity": None,
        "tokens": 0.0,
        "last_refill_at": time.monotonic(),
        "waiters": [],
        "waiter_cap": _DEFAULT_WAITER_CAP,
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
            raise ValueError(f"throttle.configure requires dict, got {inp!r}")
        rate = inp.get("refill_rate")
        cap = inp.get("capacity")
        if rate is None or cap is None:
            raise ValueError(f"throttle.configure requires refill_rate AND capacity, got {inp!r}")
        state["refill_rate"] = float(rate)
        state["capacity"] = int(cap)
        state["tokens"] = float(cap)
        state["last_refill_at"] = time.monotonic()
        state["waiters"] = state.get("waiters") or []
        state["waiter_cap"] = int(inp.get("waiter_cap", _DEFAULT_WAITER_CAP))
        context.set_state(state)
        context.set_result({"configured": True, "refill_rate": state["refill_rate"], "capacity": state["capacity"]})
        return

    if state["refill_rate"] is None:
        if op == "peek":
            context.set_result({"configured": False})
            return
        raise ValueError(f"throttle: entity not configured, cannot {op}")

    now = time.monotonic()
    elapsed = max(0.0, now - state["last_refill_at"])
    state["tokens"] = min(
        float(state["capacity"]),
        state["tokens"] + elapsed * state["refill_rate"],
    )
    state["last_refill_at"] = now

    if op == "request_permission":
        n = int(inp.get("n", 1))
        request_id = inp["request_id"]
        callback_instance_id = inp["callback_instance_id"]
        if state["tokens"] >= n:
            state["tokens"] -= n
            context.set_state(state)
            _enqueue_grant(callback_instance_id, request_id, n)
            context.set_result({"granted": True, "queued": False})
            return
        waiters = state.get("waiters") or []
        cap = int(state.get("waiter_cap", _DEFAULT_WAITER_CAP))
        if len(waiters) >= cap:
            context.set_state(state)
            raise RuntimeError(
                f"throttle: waiters list at cap ({cap}); rejecting request_id={request_id!r}"
            )
        waiters.append({
            "request_id": request_id,
            "callback_instance_id": callback_instance_id,
            "n": n,
            "enqueued_at": time.monotonic(),
        })
        state["waiters"] = waiters
        context.set_state(state)
        context.set_result({"granted": False, "queued": True, "position": len(waiters)})
        return

    if op == "tick":
        waiters = state.get("waiters") or []
        granted = []
        remaining = []
        for w in waiters:
            n = int(w.get("n", 1))
            if state["tokens"] >= n:
                state["tokens"] -= n
                granted.append(w)
            else:
                remaining.append(w)
        state["waiters"] = remaining
        context.set_state(state)
        for w in granted:
            _enqueue_grant(w["callback_instance_id"], w["request_id"], int(w.get("n", 1)))
        context.set_result({"granted": len(granted), "still_waiting": len(remaining)})
        return

    if op == "peek":
        context.set_state(state)
        context.set_result({
            "configured": True,
            "refill_rate": state["refill_rate"],
            "capacity": state["capacity"],
            "tokens": state["tokens"],
            "waiters": len(state.get("waiters") or []),
            "waiter_cap": state.get("waiter_cap", _DEFAULT_WAITER_CAP),
        })
        return

    raise ValueError(f"throttle: unknown operation {op!r}")
