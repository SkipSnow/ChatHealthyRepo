# Copyright (c) 2026 Skip Snow. All rights reserved.
# Tests for EVAL-CACHE.

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from evaluate_care.cache import ScoringCache
from evaluate_care.models import (
    CacheKey,
    EntityType,
    MeasureInput,
    ScoringRequest,
    ScoringResponse,
    WeightConfig,
    WeightEntry,
)


def _make_request(entity_id="prov-1", value=1.0) -> ScoringRequest:
    return ScoringRequest(
        entity_id=entity_id,
        entity_type=EntityType.PROVIDER,
        measures=[MeasureInput(name="board_certification", value=value)],
        weights=WeightConfig(
            weights=[WeightEntry(measure_name="board_certification", weight=1.0)]
        ),
    )


def _make_response(entity_id="prov-1") -> ScoringResponse:
    return ScoringResponse(
        entity_id=entity_id,
        entity_type=EntityType.PROVIDER,
        composite_score=0.85,
    )


class TestCacheKey:
    def test_deterministic(self):
        req = _make_request()
        k1 = CacheKey.build(req)
        k2 = CacheKey.build(req)
        assert k1.key == k2.key

    def test_different_inputs_different_keys(self):
        k1 = CacheKey.build(_make_request(entity_id="a"))
        k2 = CacheKey.build(_make_request(entity_id="b"))
        assert k1.key != k2.key


class TestScoringCache:
    def test_put_and_get(self):
        cache = ScoringCache()
        req = _make_request()
        resp = _make_response()
        cache.put(req, resp)
        result = cache.get(req)
        assert result is not None
        assert result.composite_score == 0.85

    def test_miss(self):
        cache = ScoringCache()
        req = _make_request()
        assert cache.get(req) is None

    def test_invalidate(self):
        cache = ScoringCache()
        req = _make_request()
        cache.put(req, _make_response())
        assert cache.invalidate(req) is True
        assert cache.get(req) is None

    def test_clear(self):
        cache = ScoringCache()
        for i in range(5):
            cache.put(_make_request(entity_id=f"p-{i}"), _make_response(f"p-{i}"))
        assert cache.size == 5
        removed = cache.clear()
        assert removed == 5
        assert cache.size == 0

    def test_lru_eviction(self):
        cache = ScoringCache(max_size=3)
        for i in range(5):
            cache.put(_make_request(entity_id=f"p-{i}", value=float(i)), _make_response(f"p-{i}"))
        assert cache.size == 3

    def test_stats(self):
        cache = ScoringCache()
        req = _make_request()
        cache.get(req)  # miss
        cache.put(req, _make_response())
        cache.get(req)  # hit
        stats = cache.stats
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5

    def test_expired_entry_returns_none(self):
        cache = ScoringCache(default_ttl=0)
        req = _make_request()
        cache.put(req, _make_response(), ttl=0)
        # TTL=0 means immediately expired
        import time
        time.sleep(0.01)
        assert cache.get(req) is None
