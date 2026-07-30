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

Scale contract: providers are streamed via cursor and processed in
bounded chunks (_PROVIDER_CHUNK). At most one chunk's pairs and writes
sit in memory at a time — memory is O(chunk), not O(state).
Persistence is bulk_write with UpdateOne ops and insert_many for
unresolvables; no per-provider update_one is issued.

Public entry point: `run_county_cascade(config, mongo, blob)`.
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


import csv
import io
import os
import time
from typing import Any, Iterable, Iterator

import requests
from pymongo import UpdateOne

from throttle_semaphore import RateLimitedConcurrencyGate, RateLimitedGate

# Retry policy for HTTP fetches in the cascade. Vendor endpoints (Census
# batch geocoder, NPPES registry, Google Maps) occasionally return 429
# (rate-limit) or 5xx (transient server failure); the old
# county_enrichment_job.py used retry-with-backoff for these instead of
# dropping the batch/address to residue on first stumble. Backoff is
# exponential: 1s, 2s, 4s.
_RETRY_MAX_ATTEMPTS = 4  # 1 initial + 3 retries
_RETRY_BACKOFF_BASE_S = 1.0
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _is_retryable_http(exc: BaseException) -> bool:
    """True iff exc is a transient failure worth retrying with backoff.
    Timeouts + connection errors + retryable HTTP status codes qualify;
    programming errors (400/401/403/404) do NOT — retrying those just
    hits the same failure faster."""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        if status in _RETRYABLE_STATUS:
            return True
    return False

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

# Chunk sizes keep memory bounded regardless of state size.
# _PROVIDER_CHUNK — providers materialized per cascade pass (~2 addresses per provider
# under most partitions → ~1000 pairs per chunk under _PROVIDER_CHUNK=500). Sized so
# Census batch chunks (batch_size, default 500) can absorb a single pass without
# fragmentation.
# _BULK_WRITE_CHUNK — bulk_write / insert_many flush cap. pymongo's own driver splits
# oversize batches, but we keep the cap explicit for backpressure.
_PROVIDER_CHUNK = 500
_BULK_WRITE_CHUNK = 1000


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
    """Normalize an address's zip/postal_code to a 5-digit US ZIP.

    Accepts: '19713' (5), '19713-4776' (ZIP+4 hyphenated), '197134776'
    (9-digit no hyphen), '5672' (short-pad to '05672'). Returns None if
    no zip present. Always returns exactly 5 characters when non-None."""
    z = addr.get("zip") or addr.get("postal_code") or ""
    z = str(z).strip()
    if not z:
        return None
    z = z.split("-", 1)[0]
    return z.zfill(5)[:5]


def _stamp_county(
    addr: dict, *,
    fips: str, source: str,
    name: str | None = None, rucc: int | None = None,
) -> None:
    """Stamp a county identity on an address.

    fips is authoritative (5-digit county FIPS). name is included when a
    stage or reverse-lookup can supply one — the crosswalk carries names
    in the (fips, name) tuple; Census-batch stampings resolve names via
    the fips_to_name reverse index; Google stampings receive names
    directly from the API.

    rucc is the USDA Rural-Urban Continuum Code (int 1-9) for the
    county's FIPS, when available. Preserved as an integer so callers
    (FindCare filters, EvaluateCare ranking) can apply their own
    threshold at query time rather than being forced into a
    pipeline-baked urban=bool. Absent when the RUCC lookup misses or
    when the stage produces a non-numeric fips (google_maps returns
    'STATE::CountyName' shape, not a numeric FIPS)."""
    entry: dict = {"fips": fips, "source": source}
    if name:
        entry["name"] = name
    if rucc is not None:
        entry["rucc"] = rucc
    addr["county"] = entry


def _stamp_coordinates(
    addr: dict, *,
    latitude: float, longitude: float, source: str, precision: str,
) -> None:
    """Stamp geo coordinates on an address for downstream mapping use.

    Stages that publish coordinates: census_batch (always
    range_interpolated — TIGER street-segment interpolation) and
    google_maps (variable: rooftop / range_interpolated /
    geometric_center / approximate — reflects Google's own confidence).

    Fields:
      - latitude / longitude: WGS84 decimal degrees (not GeoJSON order).
      - source: which API produced the point (census_batch | google_maps).
      - precision: qualitative accuracy bucket, one of rooftop |
        range_interpolated | geometric_center | approximate."""
    addr["coordinates"] = {
        "latitude": float(latitude),
        "longitude": float(longitude),
        "source": source,
        "precision": precision,
    }


def _stream_provider_chunks(
    coll,
    run_id: str,
    *,
    partition_state: str | None,
    partition_kind: str | None,
) -> Iterator[list[tuple[dict, list[dict]]]]:
    """Yield chunks of (provider_doc, eligible_addresses[]) tuples.

    Streams providers via cursor.batch_size — never materializes the full
    partition list. Each chunk holds _PROVIDER_CHUNK providers with only
    their enrichment-eligible addresses attached (references into the
    same doc.addresses list, so mutations propagate).
    """
    query: dict[str, Any] = {"run_id": run_id}
    match: dict[str, Any] = {"address_type": {"$in": list(PRACTICE_ADDRESS_TYPES)}}
    if partition_kind:
        match["address_type"] = partition_kind
    if partition_state:
        match["state"] = partition_state
    query["addresses"] = {"$elemMatch": match}

    cursor = coll.find(query).batch_size(_PROVIDER_CHUNK)
    chunk: list[tuple[dict, list[dict]]] = []
    for doc in cursor:
        eligible: list[dict] = []
        for addr in (doc.get("addresses") or []):
            if partition_state and (addr.get("state") or "").upper() != partition_state:
                continue
            if partition_kind and addr.get("address_type") != partition_kind:
                continue
            if _addr_needs_enrichment(addr):
                eligible.append(addr)
        if not eligible:
            continue
        chunk.append((doc, eligible))
        if len(chunk) >= _PROVIDER_CHUNK:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _pairs_from_chunk(
    chunk: list[tuple[dict, list[dict]]],
) -> list[tuple[dict, dict]]:
    """Flatten a provider chunk into (doc, addr) pairs for cascade stages."""
    pairs: list[tuple[dict, dict]] = []
    for doc, addrs in chunk:
        for addr in addrs:
            pairs.append((doc, addr))
    return pairs


def _load_zcta_crosswalk(mongo, env_prefix: str, run_id: str, data_version: int) -> dict[str, tuple[str, str]]:
    """Load the ZCTA-to-County crosswalk into an in-memory dict.

    Reads from PublicStaging.StagingCensusZctaCounty_v_{data_version} per
    the current staging convention (staging_loader.staging_collection_name).

    Contract: returns {5-digit ZIP -> (5-digit county FIPS, county name)}.
    The Census 2020 ZCTA↔County file publishes GEOID_ZCTA5_20 (ZIP),
    GEOID_COUNTY_20 (county FIPS), and NAMELSAD_COUNTY_20 (human-readable
    county name, e.g. 'New Castle County'). All three are captured so
    downstream stampings can include the name (needed for UX display of
    county alongside the numeric FIPS).

    Also accepts legacy unsuffixed keys so the loader stays resilient if
    the source ever ships without the _20 suffix. Name is optional at
    the row level — a row without a name still contributes its FIPS,
    with '' as the name."""
    from staging_loader import STAGING_DB_NAME, staging_collection_name
    coll_name = staging_collection_name("census_zcta_county", data_version)
    coll = mongo[STAGING_DB_NAME][coll_name]
    scanned = 0
    key_misses = 0
    out: dict[str, tuple[str, str]] = {}
    for row in coll.find({"run_id": run_id}):
        scanned += 1
        raw = row.get("raw") or {}
        z = (
            row.get("zcta5")
            or raw.get("GEOID_ZCTA5_20")
            or raw.get("ZCTA5")
            or raw.get("ZCTA")
            or ""
        )
        z = str(z).strip().split("-", 1)[0]
        if not z:
            key_misses += 1
            continue
        # ZIP is the FIRST 5 chars — same normalization _zip5 uses for
        # address-side lookups. Consistency between the crosswalk keys
        # and the crosswalk-lookup keys is a correctness requirement.
        z = z.zfill(5)[:5]
        fips_raw = (
            raw.get("GEOID_COUNTY_20")
            or raw.get("GEOID")
            or raw.get("COUNTY_FIPS")
            or ""
        )
        fips = str(fips_raw).strip()
        if not fips:
            key_misses += 1
            continue
        # County FIPS = state FIPS (2) + county FIPS (3) = first 5 chars.
        fips = fips.zfill(5)[:5]
        # County name column: NAMELSAD_COUNTY_20 in the Census 2020
        # ZCTA↔County national relationship file. Falls back to unsuffixed
        # legacy names if the source layout changes.
        name = str(
            raw.get("NAMELSAD_COUNTY_20")
            or raw.get("NAME_COUNTY_20")
            or raw.get("NAME")
            or raw.get("COUNTY_NAME")
            or ""
        ).strip()
        out.setdefault(z, (fips, name))
    _log.info(
        "county_cascade: crosswalk load coll=%s.%s scanned=%d loaded=%d key_misses=%d sample_raw_keys=%s",
        STAGING_DB_NAME, coll_name, scanned, len(out), key_misses,
        list((coll.find_one({"run_id": run_id}) or {}).get("raw", {}).keys())[:10] if scanned else [],
    )
    if scanned and not out:
        _log.warning(
            "county_cascade: crosswalk load returned 0 usable rows from %d staging rows — "
            "check raw-key names (looking for GEOID_ZCTA5_20/GEOID_COUNTY_20)",
            scanned,
        )
    return out


def _load_rucc_by_fips(mongo, run_id: str, data_version: int) -> dict[str, int]:
    """Load {5-digit county FIPS -> USDA RUCC integer 1..9} from staging.

    Reads from PublicStaging.StagingUsdaRucc_v_{data_version}. Preserves
    the raw integer (1..9) so downstream consumers can apply their own
    urban/rural threshold at query time — no boolean collapse here.

    RUCC integer semantics (from USDA ERS):
      1: Metro 1M+          6: Nonmetro 2.5K-20K adjacent
      2: Metro 250K-1M      7: Nonmetro 2.5K-20K nonadjacent
      3: Metro <250K        8: Nonmetro <2.5K adjacent
      4: Nonmetro 20K+ adj  9: Nonmetro <2.5K nonadjacent
      5: Nonmetro 20K+ non-adj"""
    from staging_loader import STAGING_DB_NAME, staging_collection_name
    coll_name = staging_collection_name("usda_rucc", data_version)
    coll = mongo[STAGING_DB_NAME][coll_name]
    scanned = 0
    out: dict[str, int] = {}
    fips_col: str | None = None
    rucc_col: str | None = None
    for row in coll.find({"run_id": run_id}):
        scanned += 1
        raw = row.get("raw") or {}
        if fips_col is None or rucc_col is None:
            for k in raw.keys():
                low = str(k).strip().lower()
                if fips_col is None and low in ("fips", "fips_code", "fipscode", "fips_txt"):
                    fips_col = k
                if rucc_col is None and (low.startswith("rucc_") or low == "rucc" or "ruralurbancontinuum" in low):
                    rucc_col = k
            if fips_col is None or rucc_col is None:
                continue
        try:
            fips = str(raw.get(fips_col) or "").strip().zfill(5)[:5]
            rucc = int(raw.get(rucc_col))
        except (TypeError, ValueError):
            continue
        if len(fips) != 5 or fips == "00000" or rucc < 1 or rucc > 9:
            continue
        out[fips] = rucc
    _log.info(
        "county_cascade: rucc load coll=%s.%s scanned=%d loaded=%d",
        STAGING_DB_NAME, coll_name, scanned, len(out),
    )
    return out


# ── Stage 1 — zip_crosswalk ────────────────────────────────────────────────
# No HTTP; the crosswalk is loaded once by _load_zcta_crosswalk before
# the funnel entry. Split is apply-only (no fetch, no parse).

def _build_fips_to_name(crosswalk: dict[str, tuple[str, str]]) -> dict[str, str]:
    """Reverse-index the crosswalk to {5-digit county FIPS -> county name}.
    Used by stages that only get a FIPS back from their upstream API
    (Census batch geocoder returns state_fips + county_fips but no name)
    so every stamped county carries name alongside fips."""
    out: dict[str, str] = {}
    for _zip, (fips, name) in crosswalk.items():
        if fips and name:
            out.setdefault(fips, name)
    return out


def _zip_crosswalk_apply(
    pairs: list[tuple[dict, dict]],
    crosswalk: dict[str, tuple[str, str]],
    rucc_by_fips: dict[str, int],
) -> tuple[list[tuple[dict, dict]], int]:
    """Pure lookup+stamp: for each pair, look up its 5-digit ZIP in the
    crosswalk dict. Hit → stamp county.fips + county.name + county.rucc
    (int 1-9 when the fips resolves in the RUCC catalog; otherwise
    omitted). Miss → route to residue."""
    residue: list[tuple[dict, dict]] = []
    hits = 0
    for doc, addr in pairs:
        z = _zip5(addr)
        if z and z in crosswalk:
            fips, name = crosswalk[z]
            _stamp_county(
                addr, fips=fips, source=ZIP_CROSSWALK,
                name=name or None, rucc=rucc_by_fips.get(fips),
            )
            hits += 1
        else:
            residue.append((doc, addr))
    return residue, hits


def _stage_zip_crosswalk(
    pairs: list[tuple[dict, dict]],
    crosswalk: dict[str, tuple[str, str]],
    rucc_by_fips: dict[str, int],
) -> tuple[list[tuple[dict, dict]], int]:
    """Stage 1 orchestrator: apply the pre-loaded crosswalk + emit log."""
    residue, hits = _zip_crosswalk_apply(pairs, crosswalk, rucc_by_fips)
    _log.info(
        "county_cascade[zip_crosswalk]: in=%d hit=%d residue=%d crosswalk_size=%d",
        len(pairs), hits, len(residue), len(crosswalk),
    )
    return residue, hits


# ── Stage 2 — census_batch ─────────────────────────────────────────────────
# Fetch = one HTTP POST per chunk. Parse = pure CSV → dict. Apply = pure
# stamp+residue. Orchestrator loops chunks and threads fetch → parse → apply.

def _census_batch_row(idx: int, addr: dict) -> str:
    """Build one CSV input row for the Census batch geocoder request body."""
    street = addr.get("line1") or addr.get("street_1") or addr.get("address_1") or ""
    city = addr.get("city") or ""
    state = addr.get("state") or ""
    zip5 = _zip5(addr) or ""
    return f"{idx},{street},{city},{state},{zip5}"


def _census_batch_body(chunk: list[tuple[dict, dict]]) -> str:
    """Pure: build the multipart-body CSV text for one chunk of pairs."""
    return "\n".join(_census_batch_row(i, addr) for i, (_doc, addr) in enumerate(chunk))


def _census_batch_fetch(
    body: str,
    *,
    session: requests.Session,
    gate: RateLimitedGate,
) -> str | None:
    """Impure I/O only: POST the pre-built CSV body to Census.
    Returns response text on success; None if the chunk failed after
    retry-with-backoff (caller treats None as 'zero matches this chunk')."""
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        gate.acquire()
        try:
            resp = session.post(
                CENSUS_BATCH_URL,
                files={"addressFile": ("chunk.csv", body, "text/csv")},
                data={"benchmark": "Public_AR_Current", "vintage": "Current_Current"},
                timeout=180,
            )
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            if attempt + 1 < _RETRY_MAX_ATTEMPTS and _is_retryable_http(exc):
                backoff = _RETRY_BACKOFF_BASE_S * (2 ** attempt)
                _log.warning(
                    "county_cascade[census_batch]: attempt %d/%d failed (%s); "
                    "sleeping %.1fs before retry",
                    attempt + 1, _RETRY_MAX_ATTEMPTS, exc, backoff,
                )
                time.sleep(backoff)
                continue
            _log.warning(
                "county_cascade[census_batch]: chunk fetch failed after "
                "%d attempt(s) (%s)", attempt + 1, exc,
            )
            return None
    return None


def _census_batch_parse(text: str | None) -> dict[int, dict]:
    """Pure: parse the Census batch geocoder response CSV.

    Response is quoted CSV where the matched-address field carries commas
    inside its quotes; csv.reader honors quoted-field boundaries so
    columns align correctly. A naive line.split(',') shifts every field
    and misidentifies match_type — that was the pre-refactor bug.

    Census 12-column response with geography:
      [0]id [1]input_addr [2]match_type [3]match_indicator
      [4]matched_addr [5]coordinates ("lng,lat" as ONE quoted field)
      [6]tigerline_id [7]side
      [8]state_fips [9]county_fips [10]tract [11]block

    Returns {row_id -> {fips, latitude, longitude}} for every 'Match' row.
    coordinates keys are omitted if the coordinates field is empty or
    fails to parse (extremely rare on a Match row). Skips No_Match /
    Tie / malformed rows entirely."""
    resolved: dict[int, dict] = {}
    if not text:
        return resolved
    reader = csv.reader(io.StringIO(text))
    for parts in reader:
        if len(parts) < 12:
            continue
        try:
            row_id = int(parts[0].strip())
        except ValueError:
            continue
        if parts[2].strip().lower() != "match":
            continue
        state_fips = parts[8].strip()
        county_fips = parts[9].strip()
        if not (state_fips and county_fips):
            continue
        entry: dict = {"fips": f"{state_fips.zfill(2)}{county_fips.zfill(3)}"}
        # Census coordinates column: "lng,lat" (LONGITUDE first). One
        # quoted CSV field so csv.reader hands it back as a single value.
        coord_raw = parts[5].strip()
        if coord_raw:
            coord_parts = coord_raw.split(",")
            if len(coord_parts) == 2:
                try:
                    entry["longitude"] = float(coord_parts[0].strip())
                    entry["latitude"] = float(coord_parts[1].strip())
                except ValueError:
                    pass
        resolved[row_id] = entry
    return resolved


def _census_batch_apply(
    chunk: list[tuple[dict, dict]],
    resolved: dict[int, dict],
    fips_to_name: dict[str, str],
    rucc_by_fips: dict[str, int],
) -> tuple[list[tuple[dict, dict]], int]:
    """Pure: stamp county.fips + county.name + county.rucc + coordinates
    on chunk[idx] for every idx in resolved; route misses to residue.
    County name comes from fips_to_name (Census API doesn't publish it),
    rucc comes from rucc_by_fips (USDA catalog). Coordinates are
    omitted if the API response didn't carry them (rare)."""
    residue: list[tuple[dict, dict]] = []
    hits = 0
    for idx, (doc, addr) in enumerate(chunk):
        if idx in resolved:
            entry = resolved[idx]
            fips = entry["fips"]
            _stamp_county(
                addr, fips=fips, source=CENSUS_BATCH,
                name=fips_to_name.get(fips) or None,
                rucc=rucc_by_fips.get(fips),
            )
            if "latitude" in entry and "longitude" in entry:
                # Census batch geocoder is always TIGER-based range
                # interpolation — no rooftop, no better precision hint.
                _stamp_coordinates(
                    addr, latitude=entry["latitude"],
                    longitude=entry["longitude"], source=CENSUS_BATCH,
                    precision="range_interpolated",
                )
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
    fips_to_name: dict[str, str],
    rucc_by_fips: dict[str, int],
) -> tuple[list[tuple[dict, dict]], int]:
    """Stage 2 orchestrator: chunk → build body → fetch → parse → apply.
    fips_to_name is the reverse index built from the crosswalk so every
    stamped county carries the human-readable name alongside the FIPS;
    rucc_by_fips lets the applier stamp the USDA RUCC integer alongside."""
    _log.info("county_cascade[census_batch]: entering in=%d batch_size=%d", len(pairs), batch_size)
    residue: list[tuple[dict, dict]] = []
    hits = 0
    for start in range(0, len(pairs), batch_size):
        chunk = pairs[start:start + batch_size]
        body = _census_batch_body(chunk)
        text = _census_batch_fetch(body, session=session, gate=gate)
        if text is None:
            residue.extend(chunk)
            continue
        resolved = _census_batch_parse(text)
        chunk_residue, chunk_hits = _census_batch_apply(chunk, resolved, fips_to_name, rucc_by_fips)
        residue.extend(chunk_residue)
        hits += chunk_hits
    _log.info(
        "county_cascade[census_batch]: in=%d hit=%d residue=%d",
        len(pairs), hits, len(residue),
    )
    return residue, hits


# ── Stage 3 — nppes_registry ───────────────────────────────────────────────
# NPPES publishes no county data. What NPPES DOES publish is the
# canonical practice address per NPI, which may be fresher / cleaner
# than what our loaded record carries (e.g., our ZIP is malformed
# 9-digit-no-hyphen "056724776"; NPPES canonical is clean 5-digit
# "05672"). Stage 3's job:
#   1. Fetch NPPES's canonical LOCATION-purpose address for the NPI.
#   2. If the canonical differs from ours, MUTATE the address in place
#      with the canonical values and tag address_refresh_provenance so
#      downstream consumers see NPPES-refreshed data, not the stale
#      record we loaded from the monthly dissemination file.
#   3. Re-look up the (now-possibly-refreshed) ZIP in the stage-1
#      crosswalk. If hit, stamp county with source=nppes_registry.
#
# Only applies to primary practice addresses (address_type=="practice").
# Secondary-practice addresses come from the pl_pfile — NPPES registry
# API doesn't carry them — so stage 3 is a no-op for those.

_ADDRESS_REFRESH_FIELDS = ("line1", "line2", "city", "state", "zip")


def _nppes_registry_fetch(
    npi: str,
    *,
    session: requests.Session,
    gate: RateLimitedConcurrencyGate,
) -> dict | None:
    """Impure I/O only: one NPPES registry lookup by NPI. Returns parsed
    JSON payload on success, None after retry-with-backoff on failure."""
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            with gate.hold():
                resp = session.get(
                    NPPES_REGISTRY_URL,
                    params={"number": npi, "version": "2.1"},
                    timeout=30,
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            if attempt + 1 < _RETRY_MAX_ATTEMPTS and _is_retryable_http(exc):
                backoff = _RETRY_BACKOFF_BASE_S * (2 ** attempt)
                _log.warning(
                    "county_cascade[nppes_registry]: %s attempt %d/%d "
                    "failed (%s); sleeping %.1fs",
                    npi, attempt + 1, _RETRY_MAX_ATTEMPTS, exc, backoff,
                )
                time.sleep(backoff)
                continue
            _log.warning(
                "county_cascade[nppes_registry]: %s failed after %d "
                "attempt(s) (%s)", npi, attempt + 1, exc,
            )
            return None
    return None


def _nppes_registry_parse(payload: dict | None) -> dict | None:
    """Pure: extract the canonical LOCATION-purpose address dict from an
    NPPES registry payload. Returns None if no LOCATION address is
    present or its postal_code is empty.

    Returned dict shape matches the provider-record address shape used
    elsewhere in the pipeline: line1, line2, city, state, zip (5-digit),
    country. NPPES field names (address_1 / address_2 / postal_code /
    country_code) are normalized here.

    ZIP normalization uses _zip5 for consistency with every other
    caller (crosswalk keys, apply comparisons, Census request rows).
    The 5-digit ZIP is the FIRST five characters, not the last."""
    if not payload:
        return None
    for r in (payload.get("results") or []):
        for a in (r.get("addresses") or []):
            if a.get("address_purpose", "").upper() != "LOCATION":
                continue
            zip5 = _zip5({"zip": a.get("postal_code")})
            if not zip5:
                continue
            return {
                "line1": (a.get("address_1") or "").strip(),
                "line2": (a.get("address_2") or "").strip(),
                "city": (a.get("city") or "").strip(),
                "state": (a.get("state") or "").strip().upper(),
                "zip": zip5,
                "country": (a.get("country_code") or "US").strip(),
            }
    return None


def _address_differs(ours: dict, canonical: dict) -> bool:
    """Pure: return True iff any of the compared refresh-fields differ
    (case- and whitespace-insensitive). ZIP is normalized to its 5-digit
    prefix before comparison — '056724776' and '05672' are considered
    the SAME ZIP, not different."""
    for f in _ADDRESS_REFRESH_FIELDS:
        if f == "zip":
            ours_v = _zip5(ours) or ""
            canonical_v = _zip5(canonical) or ""
        else:
            ours_v = str(ours.get(f) or "").strip().upper()
            canonical_v = str(canonical.get(f) or "").strip().upper()
        if ours_v != canonical_v:
            return True
    return False


def _apply_address_refresh(addr: dict, canonical: dict) -> None:
    """Mutate addr in place: overwrite each refresh-field with the
    canonical NPPES value, tag with provenance so downstream consumers
    know the record was refreshed from the live NPPES registry rather
    than the monthly dissemination file."""
    for f in _ADDRESS_REFRESH_FIELDS:
        v = canonical.get(f)
        if v:
            addr[f] = v
    addr["address_refresh_provenance"] = NPPES_REGISTRY


def _nppes_registry_refresh_pair(
    doc: dict,
    addr: dict,
    *,
    session: requests.Session,
    gate: RateLimitedConcurrencyGate,
) -> bool:
    """Impure. If the address is primary-practice and NPPES has a
    canonical LOCATION address that differs from ours, mutate addr in
    place with the canonical values (and tag address_refresh_provenance).
    Returns True iff we actually mutated addr. False otherwise (not
    practice-type, no NPI, no NPPES payload, canonical missing, or
    canonical identical to ours).

    Failure-safety: any exception is caught, logged, and — if addr was
    already partially mutated — rolled back from a shallow snapshot.
    The address is either fully-refreshed or exactly-as-received-in.
    Never partially-mutated."""
    if addr.get("address_type") != "practice":
        return False
    npi = doc.get("npi") or doc.get("NPI")
    if not npi:
        return False
    try:
        payload = _nppes_registry_fetch(str(npi), session=session, gate=gate)
        canonical = _nppes_registry_parse(payload)
        if not canonical:
            return False
        if not _address_differs(addr, canonical):
            return False
        snapshot = {k: v for k, v in addr.items()}
        try:
            _apply_address_refresh(addr, canonical)
            return True
        except Exception as inner_exc:
            # Mutation-time failure — restore snapshot so addr isn't
            # left half-refreshed on disk after the pipeline persists it.
            addr.clear()
            addr.update(snapshot)
            _log.warning(
                "county_cascade[nppes_registry]: refresh mutation failed "
                "for NPI %s, rolled back (%s)", npi, inner_exc,
            )
            return False
    except Exception as exc:
        _log.warning(
            "county_cascade[nppes_registry]: refresh raised for NPI %s "
            "(%s); leaving address unchanged", npi, exc,
        )
        return False


def _stage_nppes_registry(
    pairs: list[tuple[dict, dict]],
    *,
    session: requests.Session,
    gate: RateLimitedConcurrencyGate,
    crosswalk: dict[str, tuple[str, str]],
    census_gate: RateLimitedGate,
    fips_to_name: dict[str, str],
    rucc_by_fips: dict[str, int],
) -> tuple[list[tuple[dict, dict]], int]:
    """Stage 3 orchestrator, three phases:
      Phase 1 — per-NPI refresh: fetch NPPES canonical LOCATION address;
                if it differs from ours, mutate addr in place (line1 /
                line2 / city / state / zip) and tag
                address_refresh_provenance=nppes_registry.
      Phase 2 — retry stage 1 (crosswalk) on the refreshed set. Free.
                Uses crosswalk's (fips, name) tuple so stamped county
                carries name.
      Phase 3 — retry stage 2 (Census batch) on the phase-2 residue.
                One POST for the whole set — no chunking loop, because
                the address-differed set is a rounding error per fire.
                Uses fips_to_name reverse index so census-produced FIPS
                gets its human name too.

    Every hit produced inside stage 3 is stamped source=nppes_registry
    because the NPPES refresh is the causal agent that unlocked it;
    address_refresh_provenance on the address preserves the enrichment
    chain. Addresses whose canonical == ours skip both retries — same
    address that already failed stages 1 & 2 would fail them again."""
    _log.info("county_cascade[nppes_registry]: entering in=%d", len(pairs))

    # Phase 1
    refreshed: list[tuple[dict, dict]] = []
    unchanged: list[tuple[dict, dict]] = []
    for doc, addr in pairs:
        if _nppes_registry_refresh_pair(doc, addr, session=session, gate=gate):
            refreshed.append((doc, addr))
        else:
            unchanged.append((doc, addr))

    # Phase 2 — crosswalk retry, free. Stamps name from crosswalk tuple.
    r1_residue: list[tuple[dict, dict]] = []
    r1_hits = 0
    for doc, addr in refreshed:
        entry = crosswalk.get(_zip5(addr) or "")
        if entry:
            fips, name = entry
            _stamp_county(
                addr, fips=fips, source=NPPES_REGISTRY,
                name=name or None, rucc=rucc_by_fips.get(fips),
            )
            r1_hits += 1
        else:
            r1_residue.append((doc, addr))

    # Phase 3 — Census batch retry, single POST (no batch-size loop).
    # Reuses the same fetch/parse functions stage 2 built. Names come
    # from the fips_to_name reverse index.
    r2_residue: list[tuple[dict, dict]] = list(r1_residue)
    r2_hits = 0
    if r1_residue:
        text = _census_batch_fetch(
            _census_batch_body(r1_residue),
            session=session, gate=census_gate,
        )
        if text is not None:
            resolved = _census_batch_parse(text)
            r2_residue = []
            for idx, (doc, addr) in enumerate(r1_residue):
                if idx in resolved:
                    entry = resolved[idx]
                    fips = entry["fips"]
                    _stamp_county(
                        addr, fips=fips, source=NPPES_REGISTRY,
                        name=fips_to_name.get(fips) or None,
                        rucc=rucc_by_fips.get(fips),
                    )
                    if "latitude" in entry and "longitude" in entry:
                        _stamp_coordinates(
                            addr, latitude=entry["latitude"],
                            longitude=entry["longitude"], source=CENSUS_BATCH,
                            precision="range_interpolated",
                        )
                    r2_hits += 1
                else:
                    r2_residue.append((doc, addr))

    total_hits = r1_hits + r2_hits
    total_residue = unchanged + r2_residue
    _log.info(
        "county_cascade[nppes_registry]: in=%d hit=%d residue=%d "
        "(refreshed=%d crosswalk_retry=%d census_retry=%d unchanged=%d)",
        len(pairs), total_hits, len(total_residue),
        len(refreshed), r1_hits, r2_hits, len(unchanged),
    )
    return total_residue, total_hits


# ── Stage 4 — google_maps ──────────────────────────────────────────────────
# Per-address paid HTTP GET. Split: address-string builder (pure), fetch
# (impure), parse (pure), lookup wrapper (impure), stage orchestrator.

def _google_maps_addr_line(addr: dict) -> str:
    """Pure: build the single-line address string sent as ?address=..."""
    parts = [
        addr.get("line1") or addr.get("street_1") or addr.get("address_1") or "",
        addr.get("city") or "",
        addr.get("state") or "",
        _zip5(addr) or "",
    ]
    return ", ".join(p for p in parts if p)


def _google_maps_fetch(
    addr_line: str,
    *,
    session: requests.Session,
    gate: RateLimitedConcurrencyGate,
    api_key: str,
) -> dict | None:
    """Impure I/O only: one Google Maps geocode call. JSON or None after
    retry-with-backoff on transient failure."""
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            with gate.hold():
                resp = session.get(
                    GOOGLE_MAPS_GEOCODE_URL,
                    params={"address": addr_line, "key": api_key},
                    timeout=30,
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            if attempt + 1 < _RETRY_MAX_ATTEMPTS and _is_retryable_http(exc):
                backoff = _RETRY_BACKOFF_BASE_S * (2 ** attempt)
                _log.warning(
                    "county_cascade[google_maps]: attempt %d/%d failed "
                    "(%s); sleeping %.1fs",
                    attempt + 1, _RETRY_MAX_ATTEMPTS, exc, backoff,
                )
                time.sleep(backoff)
                continue
            _log.warning(
                "county_cascade[google_maps]: query failed after %d "
                "attempt(s) (%s)", attempt + 1, exc,
            )
            return None
    return None


_GOOGLE_PRECISION_MAP = {
    "ROOFTOP": "rooftop",
    "RANGE_INTERPOLATED": "range_interpolated",
    "GEOMETRIC_CENTER": "geometric_center",
    "APPROXIMATE": "approximate",
}


def _google_maps_parse(payload: dict | None) -> dict | None:
    """Pure: extract fips_key + county_name + coordinates from a Google
    Maps geocode response. Returns None on no county match. Coordinates
    keys (latitude/longitude/precision) are included when the payload
    carries geometry; omitted otherwise (rare on a successful geocode).

    fips_key is 'STATE::County Name' since Google returns no numeric
    FIPS. Downstream consumers can key on this like any other source
    label."""
    if not payload:
        return None
    for result in (payload.get("results") or []):
        county_name = None
        state_short = None
        for comp in (result.get("address_components") or []):
            types = comp.get("types") or []
            if "administrative_area_level_2" in types:
                county_name = comp.get("long_name")
            if "administrative_area_level_1" in types:
                state_short = comp.get("short_name")
        if not county_name:
            continue
        entry: dict = {
            "fips_key": f"{state_short or ''}::{county_name}",
            "name": county_name,
        }
        geometry = result.get("geometry") or {}
        location = geometry.get("location") or {}
        lat = location.get("lat")
        lng = location.get("lng")
        if lat is not None and lng is not None:
            entry["latitude"] = float(lat)
            entry["longitude"] = float(lng)
            entry["precision"] = _GOOGLE_PRECISION_MAP.get(
                (geometry.get("location_type") or "").upper(),
                "approximate",
            )
        return entry
    return None


def _google_maps_lookup(
    addr: dict,
    *,
    session: requests.Session,
    gate: RateLimitedConcurrencyGate,
    api_key: str,
) -> dict | None:
    """Impure wrapper: build addr line, fetch, parse."""
    payload = _google_maps_fetch(
        _google_maps_addr_line(addr),
        session=session, gate=gate, api_key=api_key,
    )
    return _google_maps_parse(payload)


def _stage_google_maps(
    pairs: list[tuple[dict, dict]],
    *,
    session: requests.Session,
    gate: RateLimitedConcurrencyGate,
    api_key: str,
) -> tuple[list[tuple[dict, dict]], int]:
    """Stage 4 orchestrator: paid terminal stage. No key → skip whole stage."""
    if not api_key:
        _log.warning("county_cascade[google_maps]: no API key; skipping stage in=%d", len(pairs))
        return pairs, 0
    _log.info("county_cascade[google_maps]: entering in=%d", len(pairs))
    residue: list[tuple[dict, dict]] = []
    hits = 0
    for doc, addr in pairs:
        entry = _google_maps_lookup(addr, session=session, gate=gate, api_key=api_key)
        if entry:
            _stamp_county(
                addr, fips=entry["fips_key"], source=GOOGLE_MAPS,
                name=entry.get("name"),
            )
            if "latitude" in entry and "longitude" in entry:
                _stamp_coordinates(
                    addr, latitude=entry["latitude"],
                    longitude=entry["longitude"], source=GOOGLE_MAPS,
                    precision=entry.get("precision", "approximate"),
                )
            hits += 1
        else:
            residue.append((doc, addr))
    _log.info(
        "county_cascade[google_maps]: in=%d hit=%d residue=%d",
        len(pairs), hits, len(residue),
    )
    return residue, hits


def _build_unresolvable_doc(run_id: str, doc: dict, addr: dict) -> dict:
    return {
        "run_id": run_id,
        "npi": doc.get("npi"),
        "reason": "county_unresolvable",
        "step": "county_enrichment",
        "state": (addr.get("state") or "").upper() or None,
        "entity_kind": "individual" if doc.get("entity_type_code") == "1" else "institutional",
        "detail": {"address": {k: addr.get(k) for k in ("street_1", "city", "state", "zip")}},
    }


def run_county_cascade(
    config: dict,
    *,
    mongo=None,
    blob=None,
) -> dict[str, Any]:
    """Enrich county on every eligible practice address in the run.

    Streams providers in chunks of _PROVIDER_CHUNK so memory is O(chunk)
    regardless of state size. Each chunk runs the full cascade, then
    persists via bulk_write / insert_many before the next chunk is read.

    Required config keys:
      - run_id                        (str)
      - env                           (str)
      - provider_collection           (str "<db>.<coll>")
      - partition_state               (str | None) — restrict scan to this state
      - partition_kind                (str | None) — "practice" | "secondary_practice"
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

    _log.info(
        "county_cascade: funnel entering run_id=%s state=%s kind=%s sla_target=%.3f google_enabled=%s "
        "google_api_key_set=%s provider_collection=%s",
        run_id, partition_state or "ALL", partition_kind or "ALL",
        sla_target, google_enabled, bool(google_api_key), config["provider_collection"],
    )

    data_version = int(config.get("data_version") or 0)
    crosswalk = _load_zcta_crosswalk(mongo, env_prefix, run_id, data_version)
    # fips_to_name reverse index: Census batch geocoder returns FIPS but
    # no name, so stages that hit via Census need this lookup to include
    # county.name on every stamped record (Skip: "you need it in every
    # record"). Built once at funnel entry from the crosswalk.
    fips_to_name = _build_fips_to_name(crosswalk)
    # rucc_by_fips: USDA RUCC integer 1-9 per county. Stamped alongside
    # fips/name on every practice address by all stages that produce a
    # numeric FIPS (crosswalk, Census, NPPES). Google's fips_key is
    # non-numeric ("STATE::County Name") so its lookups yield None and
    # rucc is simply omitted from Google-stamped counties.
    rucc_by_fips = _load_rucc_by_fips(mongo, run_id, data_version)
    session = requests.Session()

    # Gates are lazily instantiated so a run that resolves entirely from the
    # crosswalk never opens an HTTP session against Census or NPPES.
    census_gate: RateLimitedGate | None = None
    nppes_gate: RateLimitedConcurrencyGate | None = None
    google_gate: RateLimitedConcurrencyGate | None = None

    stage_hits = {ZIP_CROSSWALK: 0, CENSUS_BATCH: 0, NPPES_REGISTRY: 0, GOOGLE_MAPS: 0}
    total = 0
    resolved_running = 0
    unresolvable_total = 0

    update_ops: list[UpdateOne] = []
    unresolvable_docs: list[dict] = []

    def _flush_updates() -> None:
        if not update_ops:
            return
        provider_coll.bulk_write(update_ops, ordered=False)
        update_ops.clear()

    def _flush_unresolvables() -> None:
        if not unresolvable_docs:
            return
        discrepancies_coll.insert_many(unresolvable_docs, ordered=False)
        unresolvable_docs.clear()

    for chunk in _stream_provider_chunks(
        provider_coll,
        run_id,
        partition_state=partition_state,
        partition_kind=partition_kind,
    ):
        pairs = _pairs_from_chunk(chunk)
        chunk_total = len(pairs)
        if not chunk_total:
            continue
        total += chunk_total

        residue, hit1 = _stage_zip_crosswalk(pairs, crosswalk, rucc_by_fips)
        stage_hits[ZIP_CROSSWALK] += hit1

        # Per-chunk SLA gate — advance only when this chunk hasn't cleared
        # the SLA on its own. Running totals decide subsequent chunks.
        def _need_more(residue_list: list) -> bool:
            resolved_in_chunk = chunk_total - len(residue_list)
            chunk_rate = resolved_in_chunk / chunk_total
            return chunk_rate < sla_target

        if residue and _need_more(residue):
            if census_gate is None:
                census_gate = RateLimitedGate(
                    rate_per_second=float(throttle_rates.get(CENSUS_BATCH, 1.0)),
                )
            residue, hit2 = _stage_census_batch(
                residue, session=session, gate=census_gate,
                batch_size=batch_size, fips_to_name=fips_to_name,
                rucc_by_fips=rucc_by_fips,
            )
            stage_hits[CENSUS_BATCH] += hit2

        if residue and _need_more(residue):
            if nppes_gate is None:
                nppes_gate = RateLimitedConcurrencyGate(
                    max_in_flight=int(concurrency.get("nppes_max_in_flight", DEFAULT_NPPES_MAX_IN_FLIGHT)),
                    rate_per_second=float(throttle_rates.get(NPPES_REGISTRY, DEFAULT_NPPES_RATE)),
                )
            # Stage 3 refreshes each address from NPPES, then retries
            # stages 1 (crosswalk) and 2 (Census) on the refreshed set
            # before letting anything flow to stage 4 (Google, paid).
            # Needs the census_gate — build it lazily if stage 2 was
            # skipped upstream by the SLA gate.
            if census_gate is None:
                census_gate = RateLimitedGate(
                    rate_per_second=float(throttle_rates.get(CENSUS_BATCH, 1.0)),
                )
            residue, hit3 = _stage_nppes_registry(
                residue, session=session, gate=nppes_gate,
                crosswalk=crosswalk, census_gate=census_gate,
                fips_to_name=fips_to_name,
                rucc_by_fips=rucc_by_fips,
            )
            stage_hits[NPPES_REGISTRY] += hit3

        if residue and google_enabled and _need_more(residue):
            if google_gate is None:
                google_gate = RateLimitedConcurrencyGate(
                    max_in_flight=int(concurrency.get("google_max_in_flight", DEFAULT_GOOGLE_MAX_IN_FLIGHT)),
                    rate_per_second=float(throttle_rates.get(GOOGLE_MAPS, DEFAULT_GOOGLE_RATE)),
                )
            residue, hit4 = _stage_google_maps(
                residue, session=session, gate=google_gate, api_key=google_api_key
            )
            stage_hits[GOOGLE_MAPS] += hit4

        resolved_running += (chunk_total - len(residue))
        unresolvable_total += len(residue)

        # Persist this chunk before moving on — every touched provider
        # gets exactly one UpdateOne carrying its full addresses[].
        for doc, _addrs in chunk:
            update_ops.append(UpdateOne(
                {"_id": doc["_id"]},
                {"$set": {"addresses": doc.get("addresses", [])}},
            ))
            if len(update_ops) >= _BULK_WRITE_CHUNK:
                _flush_updates()

        for doc, addr in residue:
            unresolvable_docs.append(_build_unresolvable_doc(run_id, doc, addr))
            if len(unresolvable_docs) >= _BULK_WRITE_CHUNK:
                _flush_unresolvables()

    _flush_updates()
    _flush_unresolvables()

    if total == 0:
        _log.warning(
            "county_cascade: funnel exiting with 0 eligible addresses "
            "(run_id=%s state=%s kind=%s) — nothing to enrich",
            run_id, partition_state or "ALL", partition_kind or "ALL",
        )
        return {
            "total_addresses": 0,
            "stage_hits": stage_hits,
            "match_rate": 1.0,
            "sla_met": True,
            "unresolvable_count": 0,
        }

    match_rate = resolved_running / total
    _log.info(
        "county_cascade: funnel done run_id=%s state=%s kind=%s total=%d "
        "stage_hits=%s match_rate=%.4f sla_met=%s unresolvable=%d",
        run_id, partition_state or "ALL", partition_kind or "ALL",
        total, stage_hits, match_rate, match_rate >= sla_target, unresolvable_total,
    )
    return {
        "total_addresses": total,
        "stage_hits": stage_hits,
        "match_rate": match_rate,
        "sla_met": match_rate >= sla_target,
        "unresolvable_count": unresolvable_total,
    }
