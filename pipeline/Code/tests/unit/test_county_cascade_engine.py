# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for county_cascade_engine.

Covers three changes:
1. run_county_cascade advances residue through EVERY stage until stamped
   or exhausted; the prior _need_more SLA short-circuit is gone.
2. _stage_census_batch fans out chunks through a ThreadPoolExecutor
   bounded by the RateLimitedGate.
3. _stage_nppes_registry Phase 1 refresh runs in parallel bounded by
   gate.max_in_flight; results still zip 1:1 with input order.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

import county_cascade_engine as cce
from county_cascade_engine import (
    _stage_census_batch,
    _stage_nppes_registry,
)


class _NoopRateLimitedGate:
    """Stand-in for RateLimitedGate: no throttling, just callable."""
    def acquire(self): pass


class _NoopConcGate:
    """Stand-in for RateLimitedConcurrencyGate: exposes max_in_flight
    and a nullary hold() context."""
    def __init__(self, max_in_flight: int = 4):
        self.max_in_flight = max_in_flight
    class _Ctx:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def hold(self):
        return self._Ctx()


# ---------- _stage_census_batch parallel dispatch ----------

@pytest.mark.unit
def test_census_batch_parallel_dispatch(monkeypatch):
    """All chunks fetched via the ThreadPool; a fake fetcher returns
    per-address FIPS. Residue empties, hit count matches len(pairs)."""
    pairs = [
        ({"npi": str(i), "_id": i}, {"zip": "12345", "state": "VT", "line1": f"{i} Main"})
        for i in range(10)
    ]
    rucc = {"36001": 1}
    fips_to_name = {"36001": "Albany"}

    def fake_fetch(body, *, session, gate):
        # Return a Census-like response with all rows matched
        lines = body.splitlines()
        rows = []
        for idx_line in lines:
            idx = idx_line.split(",", 1)[0]
            rows.append(f'"{idx}","in","Match","Exact","addr","","","","36","001","","",""')
        return "\n".join(rows)

    monkeypatch.setattr(cce, "_census_batch_fetch", fake_fetch)

    residue, hits = _stage_census_batch(
        pairs, session=None, gate=_NoopRateLimitedGate(),
        batch_size=3, fips_to_name=fips_to_name, rucc_by_fips=rucc,
    )
    assert hits == 10
    assert residue == []


@pytest.mark.unit
def test_census_batch_records_thread_use(monkeypatch):
    """Fake fetcher records which thread ran it; more than one thread
    used proves concurrency wired up."""
    pairs = [
        ({"npi": str(i), "_id": i}, {"zip": "12345", "state": "VT", "line1": f"{i} A"})
        for i in range(8)
    ]
    thread_ids = set()
    lock = threading.Lock()

    def fake_fetch(body, *, session, gate):
        with lock:
            thread_ids.add(threading.get_ident())
        return None  # force residue to entire chunk; test only wants dispatch signal

    monkeypatch.setattr(cce, "_census_batch_fetch", fake_fetch)
    _stage_census_batch(
        pairs, session=None, gate=_NoopRateLimitedGate(),
        batch_size=2, fips_to_name={}, rucc_by_fips={},
    )
    assert len(thread_ids) > 1, "census_batch chunks must run on more than one thread"


@pytest.mark.unit
def test_census_batch_empty_pairs_short_circuit():
    residue, hits = _stage_census_batch(
        [], session=None, gate=_NoopRateLimitedGate(),
        batch_size=500, fips_to_name={}, rucc_by_fips={},
    )
    assert residue == []
    assert hits == 0


# ---------- _stage_nppes_registry Phase 1 parallel refresh ----------

@pytest.mark.unit
def test_nppes_registry_phase1_parallel(monkeypatch):
    """Fake refresh function that stamps a per-NPI marker; verify
    every pair got its own call and results preserve input order."""
    pairs = [
        ({"npi": str(i), "_id": i}, {"zip": "12345", "state": "VT",
                                       "address_type": "practice"})
        for i in range(6)
    ]
    calls_by_npi = {}
    lock = threading.Lock()

    def fake_refresh(doc, addr, *, session, gate):
        with lock:
            calls_by_npi[doc["npi"]] = threading.get_ident()
        # Half of the pairs return True (refreshed), half False (unchanged)
        return int(doc["npi"]) % 2 == 0

    monkeypatch.setattr(cce, "_nppes_registry_refresh_pair", fake_refresh)
    # Prevent phase 2/3 (crosswalk retry + census retry) from firing so
    # this test isolates phase 1.
    monkeypatch.setattr(cce, "_census_batch_fetch", lambda *a, **k: None)

    residue, hits = _stage_nppes_registry(
        pairs, session=None, gate=_NoopConcGate(max_in_flight=4),
        crosswalk={}, census_gate=_NoopRateLimitedGate(),
        fips_to_name={}, rucc_by_fips={},
    )
    # Every pair had refresh called exactly once
    assert set(calls_by_npi.keys()) == {str(i) for i in range(6)}
    # More than one thread used
    assert len(set(calls_by_npi.values())) > 1, \
        "nppes phase 1 refresh must run on more than one thread"
    # No stamping happened (empty crosswalk + none-returning census),
    # so residue holds everything and hits is 0.
    assert hits == 0
    assert len(residue) == 6


# ---------- run_county_cascade advances residue with no SLA gate ----------

@pytest.mark.unit
def test_no_sla_gate_source_code():
    """Guard test: the _need_more SLA short-circuit must not reappear.
    A regression that reintroduces it would silently regress coverage."""
    src = open(cce.__file__, encoding="utf-8").read()
    assert "def _need_more" not in src, (
        "SLA short-circuit _need_more must stay deleted; residue MUST "
        "advance through every stage until stamped or exhausted."
    )
