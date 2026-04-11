# Copyright (c) 2026 Skip Snow. All rights reserved.
# EVAL-CACHE — in-memory LRU cache for scored results.

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from .models import CacheEntry, CacheKey, ScoringRequest, ScoringResponse


class ScoringCache:
    """Thread-safe LRU cache for ScoringResponse objects.

    Deterministic: same ScoringRequest always produces the same CacheKey.
    TTL-based expiry checked on read.
    """

    def __init__(self, max_size: int = 1024, default_ttl: int = 3600):
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, request: ScoringRequest) -> Optional[ScoringResponse]:
        """Look up a cached response. Returns None on miss or expiry."""
        key = CacheKey.build(request).key
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            # Check TTL
            created = datetime.fromisoformat(entry.created_at)
            age = (datetime.now(timezone.utc) - created).total_seconds()
            if age > entry.ttl_seconds:
                del self._store[key]
                self._misses += 1
                return None
            # Move to end (most recently used)
            self._store.move_to_end(key)
            self._hits += 1
            return entry.response

    def put(
        self,
        request: ScoringRequest,
        response: ScoringResponse,
        ttl: Optional[int] = None,
    ) -> str:
        """Store a scored response. Returns the cache key."""
        cache_key = CacheKey.build(request)
        entry = CacheEntry(
            key=cache_key.key,
            response=response,
            ttl_seconds=ttl if ttl is not None else self._default_ttl,
        )
        with self._lock:
            if cache_key.key in self._store:
                self._store.move_to_end(cache_key.key)
                self._store[cache_key.key] = entry
            else:
                self._store[cache_key.key] = entry
                if len(self._store) > self._max_size:
                    self._store.popitem(last=False)
        return cache_key.key
    def clear(self) -> int:
        """Remove all entries. Returns count of removed entries."""
        with self._lock:
            count = len(self._store)
            self._store.clear()
            return count

    @property
    def size(self) -> int:
        return len(self._store)

    @property
    def stats(self) -> dict:
        return {
            "size": self.size,
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": (
                round(self._hits / (self._hits + self._misses), 4)
                if (self._hits + self._misses) > 0
                else 0.0
            ),
        }
