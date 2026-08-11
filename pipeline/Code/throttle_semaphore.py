# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""In-process request-rate throttle utility.

Realizes the concurrency-bound and rate-bound obligation on:
  - EPIC-010-F-102-S-003-REQ-T-003 (source-gather concurrency bounded by throttle)
  - EPIC-010-F-103-S-004-REQ-B-001..REQ-B-008 (county cascade vendor calls
    including NPPES registry at ~5 req/s per IP and Google Maps at the
    paid-throttled tier)

Two independent primitives:

  ConcurrencyBoundedSemaphore
    A classic counting semaphore. Caller holds the slot for the entire
    request; releases on completion. Guarantees at most N in-flight
    operations at any time.

  RateLimitedGate
    Deterministic token bucket. `acquire()` blocks the caller thread
    until at least one token has been minted for the current window,
    then consumes one token. Guarantees no more than R operations per
    time-window across all callers of this instance.

No external state — no Redis, no Mongo, no file locks. Every
throttle instance is scoped to its own process. Callers that need
cross-process coordination construct one gate per worker.
"""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException


import threading
import time
from contextlib import contextmanager
from typing import Iterator

_log = ChatHealthyLoggingService()


class ConcurrencyBoundedSemaphore:
    """At most `max_in_flight` concurrent holders across all callers."""

    def __init__(self, max_in_flight: int) -> None:
        if max_in_flight < 1:
            raise ChatHealthyException(mode="value_error", message=f"max_in_flight must be >= 1, got {max_in_flight}")
        self._sem = threading.BoundedSemaphore(value=max_in_flight)
        self._max_in_flight = max_in_flight

    @property
    def max_in_flight(self) -> int:
        return self._max_in_flight

    @contextmanager
    def hold(self, timeout: float | None = None) -> Iterator[None]:
        acquired = self._sem.acquire(timeout=timeout) if timeout is not None else self._sem.acquire()
        if not acquired:
            raise TimeoutError(
                f"ConcurrencyBoundedSemaphore.hold(): timed out after {timeout}s"
            )
        try:
            yield
        finally:
            self._sem.release()


class RateLimitedGate:
    """Steady-rate token bucket: at most `rate_per_second` acquires/sec."""

    def __init__(self, rate_per_second: float, burst: int | None = None) -> None:
        if rate_per_second <= 0:
            raise ChatHealthyException(mode="value_error", message=f"rate_per_second must be > 0, got {rate_per_second}")
        self._rate = float(rate_per_second)
        self._capacity = float(burst if burst is not None else max(1, int(rate_per_second)))
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def acquire(self, tokens: float = 1.0) -> None:
        """Block until `tokens` are available, then consume them."""
        if tokens <= 0:
            raise ChatHealthyException(mode="value_error", message=f"tokens must be > 0, got {tokens}")
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                shortfall = tokens - self._tokens
                sleep_for = shortfall / self._rate
            time.sleep(max(sleep_for, 0.001))


class RateLimitedConcurrencyGate:
    """Composite: hold a concurrency slot AND consume a rate token."""

    def __init__(self, *, max_in_flight: int, rate_per_second: float) -> None:
        self._sem = ConcurrencyBoundedSemaphore(max_in_flight=max_in_flight)
        self._gate = RateLimitedGate(rate_per_second=rate_per_second)

    @property
    def max_in_flight(self) -> int:
        return self._sem.max_in_flight

    @contextmanager
    def hold(self, timeout: float | None = None) -> Iterator[None]:
        with self._sem.hold(timeout=timeout):
            self._gate.acquire()
            yield
