# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""County enrichment cascade for the Provider Pipeline — LLD v23 §4.13.

Attaches county identity (fips + name + source) to every eligible practice
address on every provider record in the run using a cost-ordered cascade.
Each stage of the cascade is tried against the residue of the previous
stage until the cumulative match-rate SLA (98%) is met or the cascade is
exhausted.

Cascade order (LLD §4.13):
  1. zip_crosswalk  — Census ZCTA-to-County table already loaded to staging
                      (`pipeline_sources_zip_county_crosswalk`). Free.
  2. census_batch   — Census geocoding batch API. Free.
  3. nppes_registry — CMS NPPES per-NPI registry, ~5 req/s per IP. Free.
  4. google_maps    — Google Maps Geocoding API. Paid; throttled.

Realizes:

  - EPIC-010-F-103-S-004-REQ-B-001  ZIP-to-county enrichment on every
                                    practice address
  - EPIC-010-F-103-S-004-REQ-B-002  cost-ordered cascade
  - EPIC-010-F-103-S-004-REQ-B-003  cumulative match-rate SLA 98%
  - EPIC-010-F-103-S-004-REQ-B-004  Census ZCTA-to-County crosswalk stage
  - EPIC-010-F-103-S-004-REQ-B-005  Census geocoding batch stage
  - EPIC-010-F-103-S-004-REQ-B-006  NPPES registry stage
  - EPIC-010-F-103-S-004-REQ-B-007  Google Maps paid stage
  - EPIC-010-F-103-S-004-REQ-B-008  stamps county.fips, county.name,
                                    county.source per address

Eligibility (LLD §4.8.3 and §4.8.4): mailing addresses are NOT eligible
for county enrichment; only practice addresses (primary_practice,
secondary_practice) are.

Idempotency: an address already carrying a fully populated `county`
object (fips + name + source) is skipped. Re-runs pick up only the
residue.

Public entry point: `run_county_cascade(config, mongo, blob)`.
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


import os
import time
from typing import Any, Iterable

import requests

from throttle_semaphore import RateLimitedConcurrencyGate, RateLimitedGate

_log = ChatHealthyLoggingService()

PRACTICE_ADDRESS_TYPES = frozenset({"practice", "secondary_practice"})
DEFAULT_SLA = 0.98
DEFAULT_BATCH_SIZE = 500
DEFAULT_NPPES_RATE = 5.0
DEFAULT_NPPES_MAX_IN_FLIGHT = 2
DEFAULT_GOOGLE_RATE = 10.0
DEFAULT_GOOGLE_MAX_IN_FLIGHT = 4

CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
NPPES_REGISTRY_URL = "https://npiregistry.cms.hhs.gov/api/"
GOOGLE_MAPS_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

ZIP_CROSSWALK = "zip_crosswalk"
CENSUS_BATCH = "census_batch"
NPPES_REGISTRY = "nppes_registry"
GOOGLE_MAPS = "google_maps"


def _addr_needs_enrichment(addr: dict) -> bool:
    if not isinstance(addr, dict):
        return False
    if addr.get("address_type") not in PRACTICE_ADDRESS_TYPES:
        return False
    c = addr.get("county")
    if isinstance(c, dict) and c.get("fips") and c.get("name"):
        return False
    if not (addr.get("zip") or addr.get("postal_code")):
        return False
    return True


def _zip5(addr: dict) -> str | None:
    z = addr.get("zip") or addr.get("postal_code") or ""
    z = str(z).strip()
    if not z:
        return None
    return z.split("-", 1)[0].zfill(5)


def _stamp_county(addr: dict, *, fips: str, name: str, source: str) -> None:
    addr["county"] = {"fips": fips, "name": name, "source": source}


def _iterate_addresses(
    coll,
    run_id: str,
    *,
    partition_state: str | None,
    partition_kind: str | None,
) -> Iterable[tuple[dict, dict]]:
    """Yield (provider_doc, address_dict) pairs eligible for enrichment."""
    query: dict[str, Any] = {"run_id": run_id}
    match: dict[str, Any] = {"address_type": {"$in": list(PRACTICE_ADDRESS_TYPES)}}
    if partition_kind:
        match["address_type"] = partition_kind
    if partition_state:
        match["state"] = partition_state
    query["addresses"] = {"$elemMatch": match}
    for doc in coll.find(query):
        for addr in (doc.get("addresses") or []):
            if partition_state and (addr.get("state") or "").upper() != partition_state:
                continue
            if partition_kind and addr.get("address_type") != partition_kind:
                continue
            if _addr_needs_enrichment(addr):
                yield doc, addr


def _load_zcta_crosswalk(mongo, env_prefix: str, run_id: str) -> dict[str, tuple[str, str]]:
    """Load the ZCTA-to-County crosswalk into an in-memory dict."""
    coll = mongo[f"{env_prefix}_PublicHealthData"]["pipeline_sources_zip_county_crosswalk"]
    out: dict[str, tuple[str, str]] = {}
    for row in coll.find({"run_id": run_id}):
        raw = row.get("raw") or {}
        z = row.get("zcta5") or raw.get("ZCTA5") or raw.get("ZCTA") or ""
        z = str(z).strip().zfill(5)
        if not z:
            continue
        fips = str(raw.get("GEOID") or raw.get("COUNTY_FIPS") or "").strip().zfill(5)
        name = str(raw.get("NAME") or raw.get("name") or raw.get("COUNTY_NAME") or "").strip()
        if fips and name:
            out.setdefault(z, (fips, name))
    return out


def _stage_zip_crosswalk(
    pairs: list[tuple[dict, dict]],
    crosswalk: dict[str, tuple[str, str]],
) -> tuple[list[tuple[dict, dict]], int]:
    """Stamp county from the crosswalk; return (residue, hits)."""
    residue: list[tuple[dict, dict]] = []
    hits = 0
    for doc, addr in pairs:
        z = _zip5(addr)
        if z and z in crosswalk:
            fips, name = crosswalk[z]
            _stamp_county(addr, fips=fips, name=name, source=ZIP_CROSSWALK)
            hits += 1
        else:
            residue.append((doc, addr))
    return residue, hits


def _stage_census_batch(
    pairs: list[tuple[dict, dict]],
    *,
    session: requests.Session,
    gate: RateLimitedGate,
    batch_size: int,
) -> tuple[list[tuple[dict, dict]], int]:
    """Census batch geocoder — free tier. Residue passed through on any failure."""
    residue: list[tuple[dict, dict]] = []
    hits = 0
    if not pairs:
        return residue, hits

    def _row(idx: int, addr: dict) -> str:
        street = addr.get("street_1") or addr.get("address_1") or ""
        city = addr.get("city") or ""
        state = addr.get("state") or ""
        zip5 = _zip5(addr) or ""
        return f"{idx},{street},{city},{state},{zip5}"

    for start in range(0, len(pairs), batch_size):
        chunk = pairs[start:start + batch_size]
        body = "\n".join(_row(i, addr) for i, (_doc, addr) in enumerate(chunk))
        gate.acquire()
        try:
            resp = session.post(
                CENSUS_BATCH_URL,
                files={"addressFile": ("chunk.csv", body, "text/csv")},
                data={"benchmark": "Public_AR_Current", "vintage": "Current_Current"},
                timeout=180,
            )
            resp.raise_for_status()
            resolved: dict[int, tuple[str, str]] = {}
            for line in resp.text.splitlines():
                parts = line.split(",")
                if len(parts) < 12:
                    continue
                try:
                    row_id = int(parts[0].strip('"'))
                except ValueError:
                    continue
                if parts[2].strip('"').lower() != "match":
                    continue
                state_fips = parts[8].strip('"')
                county_fips = parts[9].strip('"')
                if not (state_fips and county_fips):
                    continue
                fips = f"{state_fips.zfill(2)}{county_fips.zfill(3)}"
                name = f"FIPS {fips}"
                resolved[row_id] = (fips, name)
            for idx, (_doc, addr) in enumerate(chunk):
                if idx in resolved:
                    fips, name = resolved[idx]
                    _stamp_county(addr, fips=fips, name=name, source=CENSUS_BATCH)
                    hits += 1
                else:
                    residue.append((_doc, addr))
        except Exception as exc:
            _log.warning("county_cascade[census_batch]: chunk failed (%s); passing residue", exc)
            residue.extend(chunk)
    return residue, hits


def _stage_nppes_registry(
    pairs: list[tuple[dict, dict]],
    *,
    session: requests.Session,
    gate: RateLimitedConcurrencyGate,
) -> tuple[list[tuple[dict, dict]], int]:
    """Per-NPI CMS NPPES registry lookup — free, ~5 req/s per IP."""
    residue: list[tuple[dict, dict]] = []
    hits = 0
    for doc, addr in pairs:
        npi = doc.get("npi") or doc.get("NPI")
        if not npi:
            residue.append((doc, addr))
            continue
        try:
            with gate.hold():
                resp = session.get(
                    NPPES_REGISTRY_URL,
                    params={"number": npi, "version": "2.1"},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            _log.warning("county_cascade[nppes_registry]: %s failed (%s)", npi, exc)
            residue.append((doc, addr))
            continue
        results = data.get("results") or []
        matched = False
        for r in results:
            for a in (r.get("addresses") or []):
                if a.get("address_purpose", "").upper() != "LOCATION":
                    continue
                azip = str(a.get("postal_code") or "").strip().split("-", 1)[0].zfill(5)
                if azip and azip == _zip5(addr):
                    fips = str(a.get("county_code") or "").strip()
                    name = str(a.get("county_name") or "").strip()
                    if fips and name:
                        _stamp_county(addr, fips=fips, name=name, source=NPPES_REGISTRY)
                        hits += 1
                        matched = True
                        break
            if matched:
                break
        if not matched:
            residue.append((doc, addr))
    return residue, hits


def _stage_google_maps(
    pairs: list[tuple[dict, dict]],
    *,
    session: requests.Session,
    gate: RateLimitedConcurrencyGate,
    api_key: str,
) -> tuple[list[tuple[dict, dict]], int]:
    """Google Maps Geocoding API — paid; the terminal cascade stage."""
    residue: list[tuple[dict, dict]] = []
    hits = 0
    if not api_key:
        _log.warning("county_cascade[google_maps]: no API key; skipping stage")
        return pairs, 0
    for doc, addr in pairs:
        parts = [
            addr.get("street_1") or addr.get("address_1") or "",
            addr.get("city") or "",
            addr.get("state") or "",
            _zip5(addr) or "",
        ]
        addr_line = ", ".join(p for p in parts if p)
        try:
            with gate.hold():
                resp = session.get(
                    GOOGLE_MAPS_GEOCODE_URL,
                    params={"address": addr_line, "key": api_key},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            _log.warning("county_cascade[google_maps]: query failed (%s)", exc)
            residue.append((doc, addr))
            continue
        matched = False
        for result in (data.get("results") or []):
            county_name = None
            state_short = None
            for comp in (result.get("address_components") or []):
                types = comp.get("types") or []
                if "administrative_area_level_2" in types:
                    county_name = comp.get("long_name")
                if "administrative_area_level_1" in types:
                    state_short = comp.get("short_name")
            if county_name:
                fips_key = f"{state_short or ''}::{county_name}"
                _stamp_county(addr, fips=fips_key, name=county_name, source=GOOGLE_MAPS)
                hits += 1
                matched = True
                break
        if not matched:
            residue.append((doc, addr))
    return residue, hits


def _persist_provider_addresses(coll, doc: dict) -> None:
    coll.update_one({"_id": doc["_id"]}, {"$set": {"addresses": doc.get("addresses", [])}})


def _record_unresolvable(
    discrepancies_coll,
    *,
    run_id: str,
    doc: dict,
    addr: dict,
) -> None:
    discrepancies_coll.insert_one({
        "run_id": run_id,
        "npi": doc.get("npi"),
        "reason": "county_unresolvable",
        "step": "county_enrichment",
        "state": (addr.get("state") or "").upper() or None,
        "entity_kind": "individual" if doc.get("entity_type_code") == "1" else "institutional",
        "detail": {"address": {k: addr.get(k) for k in ("street_1", "city", "state", "zip")}},
    })


def run_county_cascade(
    config: dict,
    *,
    mongo=None,
    blob=None,
) -> dict[str, Any]:
    """Enrich county on every eligible practice address in the run.

    Required config keys:
      - run_id                        (str)
      - env                           (str)
      - provider_collection           (str "<db>.<coll>")
      - partition_state               (str | None) — restrict scan to this state
      - partition_kind                (str | None) — "primary_practice" | "secondary_practice"
      - sla_target                    (float, default 0.98)
      - google_maps_api_key           (str) — required to enable google_maps stage
      - google_maps_enabled           (bool, default False)
      - throttle_rates                (dict):
            nppes_registry            (float, default 5.0)
            google_maps               (float, default 10.0)
            census_batch              (float, default 1.0)
      - concurrency:
            nppes_max_in_flight       (int, default 2)
            google_max_in_flight      (int, default 4)
      - batch_size                    (int, default 500) — census batch chunk size

    Returns metrics dict:
      {
        "total_addresses": int,
        "stage_hits": {stage: int},
        "match_rate": float,
        "sla_met": bool,
        "unresolvable_count": int,
      }
    """
    run_id = config["run_id"]
    env_prefix = config["env"]
    partition_state = (config.get("partition_state") or "").upper() or None
    partition_kind = config.get("partition_kind")
    sla_target = float(config.get("sla_target", DEFAULT_SLA))
    throttle_rates = config.get("throttle_rates") or {}
    concurrency = config.get("concurrency") or {}
    batch_size = int(config.get("batch_size", DEFAULT_BATCH_SIZE))
    google_enabled = bool(config.get("google_maps_enabled"))
    google_api_key = config.get("google_maps_api_key") or os.environ.get("GOOGLE_MAPS_API_KEY", "")

    db_name, coll_name = config["provider_collection"].split(".", 1)
    provider_coll = mongo[db_name][coll_name]
    discrepancies_coll = mongo["chathealthyfrontend"]["pipeline.discrepancies"]

    pairs = list(_iterate_addresses(
        provider_coll,
        run_id,
        partition_state=partition_state,
        partition_kind=partition_kind,
    ))
    total = len(pairs)
    stage_hits = {ZIP_CROSSWALK: 0, CENSUS_BATCH: 0, NPPES_REGISTRY: 0, GOOGLE_MAPS: 0}

    if total == 0:
        return {
            "total_addresses": 0,
            "stage_hits": stage_hits,
            "match_rate": 1.0,
            "sla_met": True,
            "unresolvable_count": 0,
        }

    crosswalk = _load_zcta_crosswalk(mongo, env_prefix, run_id)
    residue, hit1 = _stage_zip_crosswalk(pairs, crosswalk)
    stage_hits[ZIP_CROSSWALK] = hit1

    session = requests.Session()

    if residue and (total - len(residue)) / total < sla_target:
        census_gate = RateLimitedGate(rate_per_second=float(throttle_rates.get(CENSUS_BATCH, 1.0)))
        residue, hit2 = _stage_census_batch(
            residue, session=session, gate=census_gate, batch_size=batch_size
        )
        stage_hits[CENSUS_BATCH] = hit2

    if residue and (total - len(residue)) / total < sla_target:
        nppes_gate = RateLimitedConcurrencyGate(
            max_in_flight=int(concurrency.get("nppes_max_in_flight", DEFAULT_NPPES_MAX_IN_FLIGHT)),
            rate_per_second=float(throttle_rates.get(NPPES_REGISTRY, DEFAULT_NPPES_RATE)),
        )
        residue, hit3 = _stage_nppes_registry(residue, session=session, gate=nppes_gate)
        stage_hits[NPPES_REGISTRY] = hit3

    if residue and google_enabled and (total - len(residue)) / total < sla_target:
        google_gate = RateLimitedConcurrencyGate(
            max_in_flight=int(concurrency.get("google_max_in_flight", DEFAULT_GOOGLE_MAX_IN_FLIGHT)),
            rate_per_second=float(throttle_rates.get(GOOGLE_MAPS, DEFAULT_GOOGLE_RATE)),
        )
        residue, hit4 = _stage_google_maps(
            residue, session=session, gate=google_gate, api_key=google_api_key
        )
        stage_hits[GOOGLE_MAPS] = hit4

    touched: dict[Any, dict] = {}
    for doc, _addr in pairs:
        touched[doc["_id"]] = doc
    for doc in touched.values():
        _persist_provider_addresses(provider_coll, doc)

    for doc, addr in residue:
        _record_unresolvable(discrepancies_coll, run_id=run_id, doc=doc, addr=addr)

    match_rate = (total - len(residue)) / total
    return {
        "total_addresses": total,
        "stage_hits": stage_hits,
        "match_rate": match_rate,
        "sla_met": match_rate >= sla_target,
        "unresolvable_count": len(residue),
    }
