# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""throttle_entities - Durable Entity token buckets for external-API rate-limiting.

Per the records-as-messages design (§8.2, §15 Modify), the canonical entity body
is now throttle_entity_fn in throttle_entity.py, which supports waiters[] and
async grant callbacks. token_bucket_entity_fn is retained here as a transitional
alias that delegates to the new generic entity body.

One Durable Entity instance per external service:
    token_bucket / nppes
    token_bucket / google_maps
    token_bucket / openai

The entity is the SINGLE SOURCE OF TRUTH for each bucket's configured
parameters (refill_rate, capacity). The parent orchestrator's preamble
signals `configure` on each bucket once per pipeline run with the operator's
CLI values; the entity's state is then peek-able through the Durable
Functions HTTP management API for ops visibility.

Workers (activities) receive the same refill_rate / capacity values inline
via their config dict (the orchestrator propagates them straight through),
and use them locally to space outbound API calls. The entity exists so
future iterations can move workers to call `acquire` for true global
cross-worker rate enforcement; the contract is stable now even though the
caller pattern (orchestrator-side gating vs activity-side gating) may
change.

Operations
----------
configure({"refill_rate": float, "capacity": int})
    Set the bucket parameters. Resets `tokens` to capacity. Idempotent.
acquire({"n": int} | int)  -> bool
    Atomically deduct n tokens if available. Returns True on success,
    False if the bucket is empty (caller backs off and retries).
    Unconfigured bucket → always returns False (fail-loud-by-no-progress).
peek()  -> dict
    Return the bucket's current state for diagnostics.
"""
from __future__ import annotations
from chathealthy_lib.exceptions import ChatHealthyException

import time


def token_bucket_entity_fn(context) -> None:
    """Legacy entrypoint - delegates to the canonical throttle_entity body."""
    from throttle_entity import throttle_entity_fn
    return throttle_entity_fn(context)


def _legacy_token_bucket_entity_fn(context) -> None:
    """Single-threaded actor body for one token bucket. Preserved for reference."""
    state = context.get_state(lambda: {
        "refill_rate":    None,   # tokens added per second; None until configured
        "capacity":       None,   # max burst (tokens)
        "tokens":         0.0,    # current count
        "last_refill_at": time.monotonic(),
    })
    op_name = context.operation_name
    op_input = context.get_input()

    if op_name == "configure":
        if not isinstance(op_input, dict):
            raise ChatHealthyException(mode="value_error", message="token_bucket.configure requires a dict with refill_rate "
                f"and capacity; got {op_input!r}")
        refill_rate = op_input.get("refill_rate")
        capacity    = op_input.get("capacity")
        if refill_rate is None or capacity is None:
            raise ChatHealthyException(mode="value_error", message="token_bucket.configure requires refill_rate AND capacity; "
                f"got {op_input!r}")
        state["refill_rate"]    = float(refill_rate)
        state["capacity"]       = int(capacity)
        state["tokens"]         = float(capacity)  # start full
        state["last_refill_at"] = time.monotonic()
        context.set_state(state)
        context.set_result({"configured": True, **op_input})
        return

    if state["refill_rate"] is None:
        # Unconfigured bucket refuses all acquire/peek dispensing. The
        # parent orchestrator MUST configure every bucket before fan-out.
        if op_name == "acquire":
            context.set_result(False)
        elif op_name == "peek":
            context.set_result({"configured": False})
        else:
            raise ChatHealthyException(mode="value_error", message=f"token_bucket: unknown operation {op_name!r}")
        return

    # Lazy refill — fold elapsed wall time into the token count, capped.
    now = time.monotonic()
    elapsed = max(0.0, now - state["last_refill_at"])
    state["tokens"] = min(
        float(state["capacity"]),
        state["tokens"] + elapsed * state["refill_rate"],
    )
    state["last_refill_at"] = now

    if op_name == "acquire":
        if isinstance(op_input, dict):
            n = int(op_input.get("n", 1))
        else:
            n = int(op_input or 1)
        if state["tokens"] >= n:
            state["tokens"] -= n
            context.set_state(state)
            context.set_result(True)
        else:
            context.set_state(state)
            context.set_result(False)
        return

    if op_name == "peek":
        context.set_state(state)
        context.set_result({
            "configured":     True,
            "refill_rate":    state["refill_rate"],
            "capacity":       state["capacity"],
            "tokens":         state["tokens"],
            "last_refill_at": state["last_refill_at"],
        })
        return

    raise ChatHealthyException(mode="value_error", message=f"token_bucket: unknown operation {op_name!r}")


# ── Helpers for orchestrators ───────────────────────────────────────────────

BUCKET_NAMES = ("nppes", "google_maps", "openai")


def call_delay_seconds(refill_rate: float) -> float:
    """Per-call delay (seconds) that an activity must sleep between
    consecutive outbound API calls to stay inside the configured refill rate.

    refill_rate = tokens/sec, so 1 token = (1 / refill_rate) seconds.
    Activities receive refill_rate from the orchestrator's config and call
    this helper to derive their local pacing delay.
    """
    if refill_rate <= 0:
        raise ChatHealthyException(mode="value_error", message=f"refill_rate must be > 0, got {refill_rate}")
    return 1.0 / float(refill_rate)
