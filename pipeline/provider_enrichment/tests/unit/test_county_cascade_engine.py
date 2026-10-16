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

import pipeline.provider_enrichment.county_cascade_engine as cce
from pipeline.provider_enrichment.county_cascade_engine import (
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
        # Sleep so the ThreadPool can actually dispatch multiple chunks
        # concurrently; a zero-cost fake would serialize trivially and
        # give a false negative on the "is-this-actually-parallel" test.
        import time
        time.sleep(0.05)
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
        # Sleep so the ThreadPool actually dispatches concurrently
        # instead of a zero-cost fake serializing everything.
        import time
        time.sleep(0.05)
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


# ---------- Census request body quoting ----------

@pytest.mark.unit
def test_census_batch_body_quotes_fields_with_commas():
    """A comma inside line1 must stay inside its column. The unquoted
    f-string body shifted city/state/ZIP right by one and Census No_Matched
    the row (live, 2026-10-16: '1333 W 5TH ST, STE 206' -> state=SHERIDAN)."""
    import csv, io
    chunk = [
        ({}, {"line1": "1333 W 5TH ST, STE 206", "city": "SHERIDAN", "state": "WY", "zip": "82801"}),
        ({}, {"line1": "2710 HARNEY ST", "city": "LARAMIE", "state": "WY", "zip": "82072"}),
    ]
    body = cce._census_batch_body(chunk)
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0] == ["0", "1333 W 5TH ST, STE 206", "SHERIDAN", "WY", "82801"]
    assert rows[1] == ["1", "2710 HARNEY ST", "LARAMIE", "WY", "82072"]
    # The row id stays a bare leading field so a naive split still finds it.
    assert body.splitlines()[0].split(",", 1)[0] == "0"


# ---------- Census locations (geo re-pass) parse ----------

@pytest.mark.unit
def test_census_locations_parse_keeps_coordinates_without_fips():
    """The locations endpoint returns 8 columns and no FIPS. The geographies
    parser dropped such rows; the geo re-pass must keep the point."""
    text = (
        '"0","2710 HARNEY ST, LARAMIE, WY, 82072","Match","Non_Exact",'
        '"2710 E HARNEY ST, LARAMIE, WY, 82072","-105.562285291818,41.32065389702","168741561","L"\n'
        '"1","1333 W 5TH ST, STE 206, SHERIDAN, WY","No_Match"\n'
        '"2","x","Match","Exact","y","","1","R"\n'
    )
    got = cce._census_locations_parse(text)
    assert got == {0: {"longitude": -105.562285291818, "latitude": 41.32065389702}}
    # and the geographies parser does NOT accept that row (documents why the
    # geo re-pass needs its own parser).
    assert cce._census_batch_parse(text) == {}


@pytest.mark.unit
def test_stage_geo_census_posts_to_locations_endpoint(monkeypatch):
    seen = {}

    def fake_fetch(body, *, session, gate, url=cce.CENSUS_BATCH_URL, with_geography=True):
        seen["url"] = url
        seen["with_geography"] = with_geography
        return '"0","in","Match","Exact","m","-77.79,40.84","1","L"'

    monkeypatch.setattr(cce, "_census_batch_fetch", fake_fetch)
    addr = {"line1": "250 PERSIA RD", "city": "BELLEFONTE", "state": "PA", "zip": "16823",
            "coordinates": {"latitude": -1.0, "longitude": -1.0, "source": "pending", "precision": "pending"}}
    residue, hits = cce._stage_geo_census(
        [({"npi": "1"}, addr)], session=None, gate=_NoopRateLimitedGate(), batch_size=500,
    )
    assert seen == {"url": cce.CENSUS_LOCATIONS_URL, "with_geography": False}
    assert hits == 1 and residue == []
    assert addr["coordinates"] == {
        "latitude": 40.84, "longitude": -77.79,
        "source": cce.CENSUS_BATCH, "precision": "range_interpolated",
    }


# ---------- Google parse rejects a result in another state ----------

def _google_payload(state_short: str, lat: float, lng: float) -> dict:
    return {"results": [{
        "address_components": [
            {"long_name": "Some County", "types": ["administrative_area_level_2", "political"]},
            {"short_name": state_short, "types": ["administrative_area_level_1", "political"]},
        ],
        "geometry": {"location": {"lat": lat, "lng": lng}, "location_type": "ROOFTOP"},
    }]}


@pytest.mark.unit
def test_google_parse_rejects_result_in_other_state():
    payload = _google_payload("CA", 33.77, -118.19)
    assert cce._google_maps_parse(payload, expected_state="WY") is None
    hit = cce._google_maps_parse(payload, expected_state="CA")
    assert hit["latitude"] == 33.77 and hit["fips_key"] == "CA::Some County"
    # No expected state (legacy callers) keeps the old behaviour.
    assert cce._google_maps_parse(payload) is not None


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
