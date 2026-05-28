# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""process_assignment_activity - the straight-through per-record worker.

For one assignment (byte range of the NPPES CSV), inline chain:
  stream -> state-filter -> normalize -> pass1 zip -> pass2 census ->
  pass3 billing -> pass4 maps -> pass6 nppes -> stamp_urban -> stamp_flags ->
  mark_quality -> should_embed? -> embed -> stage (one blob per record).

At assignment close: list-by-prefix, read each, bulk_write insert, delete blobs.
Pre-condition: the streaming_pipeline_orchestrator has called drain_staging
for the requested states, so the target slice is empty and inserts are clean.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from pymongo import MongoClient, InsertOne


# ── In-process per-API rate limiters ──────────────────────────────────────────
# Each external API has its own token bucket enforced inside this activity
# process. The Durable @throttle@ entities are no longer used for per-API
# rate control; they now gate only chunk-level concurrency (@throttle@pool_size)
# and source-gather startup (@throttle@source_gather). The ACA pipeline runs
# scale-to-1, so a process-local bucket is globally authoritative.
class _TokenBucket:
    """Threadsafe token bucket. acquire(n) blocks until n tokens are available."""
    def __init__(self, rate_per_sec: float, burst: int):
        self.rate = float(rate_per_sec)
        self.burst = float(burst)
        self.tokens = float(burst)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, n: int = 1, max_wait_seconds: float = 600.0) -> None:
        if n < 1:
            return
        if n > self.burst:
            raise ValueError(
                f"_TokenBucket.acquire: n={n} exceeds burst={self.burst}; caller must split"
            )
        deadline = time.monotonic() + max_wait_seconds
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(
                    self.burst,
                    self.tokens + (now - self.last) * self.rate,
                )
                self.last = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                deficit = n - self.tokens
                wait_s = deficit / self.rate if self.rate > 0 else 1.0
            wait_s = max(0.005, min(wait_s, 1.0))
            if time.monotonic() + wait_s > deadline:
                raise TimeoutError(
                    f"_TokenBucket.acquire: n={n} not granted within {max_wait_seconds}s"
                )
            time.sleep(wait_s)


_CENSUS = _TokenBucket(rate_per_sec=100.0,  burst=200)
_GMAPS  = _TokenBucket(rate_per_sec=50.0,   burst=100)
_NPPES  = _TokenBucket(rate_per_sec=20.0,   burst=50)
# _OPENAI counts INPUTS embedded (not requests). One batched call burns
# n=len(group) tokens, where group <= _EMBED_BATCH_SIZE. Sized so a typical
# sub-batch (500 inputs) drains less than one second of bucket.
_OPENAI = _TokenBucket(rate_per_sec=1000.0, burst=5000)

# ── Module-level address cache ────────────────────────────────────────────────
# Same office address appears for many providers; once Census/Maps/NPPES
# resolves it once, every future record at that address skips the API call.
# Process-local (single ACA replica); thread-safe. Capped to avoid unbounded
# growth on long runs.
import threading as _threading_for_cache
_ADDR_CACHE_LOCK = _threading_for_cache.Lock()
_ADDR_CACHE: dict[str, dict] = {}
_ADDR_CACHE_CAP = 200_000


def _addr_key(addr: dict) -> str:
    line1 = (addr.get("line1") or "").strip().upper()
    city = (addr.get("city") or "").strip().upper()
    state = (addr.get("state") or "").strip().upper()
    zip5 = (addr.get("zip") or "")[:5]
    return f"{line1}|{city}|{state}|{zip5}"


def _addr_cache_get(addr: dict) -> dict | None:
    k = _addr_key(addr)
    with _ADDR_CACHE_LOCK:
        return _ADDR_CACHE.get(k)


def _addr_cache_put(addr: dict, county_doc: dict) -> None:
    if not county_doc or not county_doc.get("fips"):
        return
    k = _addr_key(addr)
    with _ADDR_CACHE_LOCK:
        if len(_ADDR_CACHE) >= _ADDR_CACHE_CAP:
            return  # cap reached; stop accepting new entries this run
        _ADDR_CACHE[k] = dict(county_doc)


_mongo: MongoClient | None = None


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(
            os.environ["MONGO_connectionString"],
            serverSelectionTimeoutMS=120_000,
            maxPoolSize=64,
        )
    return _mongo


def _providers_coll(config: dict):
    fqn = config.get("provider_collection", f"{os.environ.get('ENV_PREFIX', 'dev')}_PublicHealthData.providers")
    db_name, coll_name = fqn.split(".", 1)
    return _get_mongo_client()[db_name][coll_name]


def _source_blob_client(config: dict):
    from blob_client import get_blob_service
    container = config.get("blob_container", "provider-data")
    csv_path = config["csv_path"]
    return get_blob_service().get_container_client(container).get_blob_client(csv_path)


# CSV streaming helpers moved verbatim from streaming_pipeline.py (now deleted).

_HEADER_CACHE: dict[str, list] = {}
_HEADER_CACHE_LOCK = threading.Lock()


def _fetch_header(blob_client) -> list:
    """Module-level cached. Key by blob URL — same ProviderPipelineRunner
    re-uses the header across all chunks; same NPPES dissemination file
    across runs re-uses too. Avoids the 32KB header download per activity."""
    key = getattr(blob_client, "url", None) or repr(blob_client)
    with _HEADER_CACHE_LOCK:
        cached = _HEADER_CACHE.get(key)
        if cached is not None:
            return cached
    header_bytes = blob_client.download_blob(offset=0, length=32768).readall()
    header_text = header_bytes.decode("utf-8", errors="replace")
    if "\n" not in header_text:
        raise RuntimeError("Header line exceeds 32KB - unexpected CSV format.")
    header_line = header_text.split("\n")[0]
    parsed = list(csv.reader([header_line]))[0]
    with _HEADER_CACHE_LOCK:
        _HEADER_CACHE[key] = parsed
    return parsed


def _iter_csv_partition(blob_client, start_byte: int, end_byte: int, header: list):
    """Stream a precise-record-boundary chunk. [start_byte, end_byte) was
    computed by chunk_indexer to start at a record's first byte and end at
    the byte after a record's terminating newline, so every \\n inside the
    payload separates complete records — no boundary fix-up needed.
    """
    payload = blob_client.download_blob(
        offset=start_byte, length=end_byte - start_byte,
    ).readall()
    text = payload.decode("utf-8", errors="replace")
    expected_field_count = len(header)
    local_id = 0
    for row in csv.reader(text.splitlines()):
        if not row or len(row) != expected_field_count:
            continue
        local_id += 1
        yield local_id, dict(zip(header, row))


_PRACTICE_STATE_COL = "Provider Business Practice Location Address State Name"
_MAILING_STATE_COL = "Provider Business Mailing Address State Name"
_LICENSE_STATE_PREFIX = "Provider License Number State Code_"
_OID_STATE_PREFIX = "Other Provider Identifier State_"


def _raw_row_matches_state(raw_row: dict, states_set: set) -> bool:
    for col in (_PRACTICE_STATE_COL, _MAILING_STATE_COL):
        v = (raw_row.get(col) or "").strip().upper()
        if v in states_set:
            return True
    for k, v in raw_row.items():
        if not v:
            continue
        if k.startswith(_LICENSE_STATE_PREFIX) or k.startswith(_OID_STATE_PREFIX):
            if v.strip().upper() in states_set:
                return True
    return False


_PASSTHROUGH_RAW_COLUMNS = {
    "NPI": "npi",
    "Entity Type Code": "entity_type_code",
    "Provider Name Prefix Text": "provider_name_prefix_text",
    "Provider First Name": "provider_first_name",
    "Provider Middle Name": "provider_middle_name",
    "Provider Last Name (Legal Name)": "provider_last_name_legal_name",
    "Provider Name Suffix Text": "provider_name_suffix_text",
    "Provider Credential Text": "provider_credential_text",
    "Provider Other Organization Name": "provider_other_organization_name",
    "Provider Other Organization Name Type Code": "provider_other_organization_name_type_code",
    "Provider Organization Name (Legal Business Name)": "provider_organization_name_legal_business_name",
    "Provider Sex Code": "provider_sex_code",
    "Is Sole Proprietor": "is_sole_proprietor",
    "Is Organization Subpart": "is_organization_subpart",
    "Parent Organization LBN": "parent_organization_lbn",
    "Parent Organization TIN": "parent_organization_tin",
    "Authorized Official Last Name": "authorized_official_last_name",
    "Authorized Official First Name": "authorized_official_first_name",
    "Authorized Official Middle Name": "authorized_official_middle_name",
    "Authorized Official Title or Position": "authorized_official_title_or_position",
    "Authorized Official Credential Text": "authorized_official_credential_text",
    "Employer Identification Number (EIN)": "employer_identification_number_ein",
    "Provider Enumeration Date": "provider_enumeration_date",
    "Last Update Date": "last_update_date",
    "Certification Date": "certification_date",
}


def _layer_passthrough_fields(raw: dict, content: dict) -> None:
    for raw_col, doc_field in _PASSTHROUGH_RAW_COLUMNS.items():
        v = (raw.get(raw_col) or "").strip()
        if v:
            content[doc_field] = v


# Per-record adapters that share the existing batch helpers' crosswalk/HTTP infra.

def _pass1_zip(doc: dict, crosswalk: dict) -> None:
    for addr in doc.get("addresses") or []:
        if not isinstance(addr, dict):
            continue
        if isinstance(addr.get("county"), dict) and addr["county"].get("fips"):
            continue
        z = (addr.get("zip") or "")[:5]
        if not z:
            continue
        match = crosswalk.get(z)
        if not match or match.get("is_split"):
            continue
        ratio = match.get("ratio") or 0
        if ratio < 0.98:
            continue
        addr["county"] = {
            "fips": match["fips"],
            "name": match.get("name") or "",
            "source": "crosswalk_pass1",
            "zip_ratio": ratio,
        }


def _addresses_needing_geocoding(doc: dict, addr_type: str | None = None) -> list:
    out = []
    for addr in doc.get("addresses") or []:
        if not isinstance(addr, dict):
            continue
        if addr_type and addr.get("address_type") != addr_type:
            continue
        county = addr.get("county") or {}
        if not county.get("fips") or county.get("source") == "geocoder_failed":
            out.append(addr)
    return out


def _try_addr_cache(addr: dict) -> bool:
    """If we've already resolved this address in-process, stamp from cache.
    Returns True when cached (skip the API call), False otherwise."""
    cached = _addr_cache_get(addr)
    if not cached:
        return False
    addr["county"] = dict(cached)
    return True


def _pass2_census(doc: dict) -> None:
    targets = _addresses_needing_geocoding(doc, addr_type="practice")
    if not targets:
        return
    # Cache hits drop out before API
    miss = [a for a in targets if not _try_addr_cache(a)]
    if not miss:
        return
    _CENSUS.acquire(n=len(miss))
    _call_census_for_addresses(doc, miss, pass_label="geocoder_pass2_batch")
    for a in miss:
        c = a.get("county") or {}
        if c.get("fips"):
            _addr_cache_put(a, c)


def _pass3_billing(doc: dict) -> None:
    targets = []
    for addr in doc.get("addresses") or []:
        if not isinstance(addr, dict):
            continue
        if addr.get("address_type") != "practice":
            continue
        county = addr.get("county") or {}
        if county.get("source") != "geocoder_failed":
            continue
        targets.append(addr)
    if not targets:
        return
    # Cache hits on the failed practice addrs themselves first
    targets = [a for a in targets if not _try_addr_cache(a)]
    if not targets:
        return
    billing_addr = None
    for addr in doc.get("addresses") or []:
        if isinstance(addr, dict) and addr.get("address_type") == "business":
            billing_addr = addr
            break
    if not billing_addr:
        return
    # Cache the billing address too if we've resolved it before
    cached_billing = _addr_cache_get(billing_addr)
    if cached_billing and cached_billing.get("fips"):
        for a in targets:
            a["county"] = {**cached_billing, "source": "geocoder_pass3_billing"}
        return
    _CENSUS.acquire(n=1)
    _call_census_for_addresses(doc, [billing_addr], pass_label="geocoder_pass3_billing", retarget=targets)
    for a in targets:
        c = a.get("county") or {}
        if c.get("fips"):
            _addr_cache_put(a, c)


def _pass4_maps(doc: dict) -> None:
    targets = []
    for addr in doc.get("addresses") or []:
        if not isinstance(addr, dict):
            continue
        if addr.get("address_type") != "practice":
            continue
        county = addr.get("county") or {}
        if county.get("source") != "geocoder_failed":
            continue
        targets.append(addr)
    if not targets:
        return
    miss = [a for a in targets if not _try_addr_cache(a)]
    if not miss:
        return
    _GMAPS.acquire(n=len(miss))
    _call_google_maps_for_addresses(doc, miss, pass_label="geocoder_pass4_maps")
    for a in miss:
        c = a.get("county") or {}
        if c.get("fips"):
            _addr_cache_put(a, c)


def _pass6_nppes(doc: dict) -> None:
    needs_last_resort = False
    for addr in doc.get("addresses") or []:
        if not isinstance(addr, dict):
            continue
        county = addr.get("county") or {}
        if county.get("source") == "geocoder_failed" or not county.get("fips"):
            needs_last_resort = True
            break
    if not needs_last_resort:
        return
    npi = doc.get("npi")
    if not npi:
        return
    _NPPES.acquire(n=1)
    _call_nppes_api_for_npi(doc, npi)
    # Populate cache from any pass6-resolved addresses
    for addr in doc.get("addresses") or []:
        if not isinstance(addr, dict):
            continue
        c = addr.get("county") or {}
        if c.get("source") == "geocoder_pass6_nppes" and c.get("fips"):
            _addr_cache_put(addr, c)


def _call_census_for_addresses(doc, addresses, pass_label, retarget=None):
    from county_enrichment_job import _get_crosswalk
    if not addresses:
        return
    lines = []
    for i, addr in enumerate(addresses):
        line1 = (addr.get("line1") or "").strip()
        city = (addr.get("city") or "").strip()
        state = (addr.get("state") or "").strip()
        zip5 = (addr.get("zip") or "")[:5]
        lines.append(f'{i},"{line1}","{city}","{state}","{zip5}"')
    payload = "Unique ID,Street address,City,State,ZIP\n" + "\n".join(lines)
    url = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
    files = {"addressFile": ("addresses.csv", payload)}
    data = {"benchmark": "Public_AR_Current", "vintage": "Current_Current"}
    try:
        resp = _http_session().post(url, files=files, data=data, timeout=180)
        resp.raise_for_status()
    except Exception as exc:
        logging.warning("census batch failed: %s", exc)
        target_list = retarget if retarget else addresses
        for addr in target_list:
            addr["county"] = {**(addr.get("county") or {}), "source": "geocoder_failed"}
        return
    cw = _get_crosswalk()
    target_list = retarget if retarget else addresses
    parsed_by_id: dict = {}
    for row in csv.reader(io.StringIO(resp.text)):
        if not row:
            continue
        rec_id = row[0]
        match_status = row[2] if len(row) > 2 else ""
        county_fips = row[12] if len(row) > 12 else ""
        if match_status == "Match" and county_fips:
            parsed_by_id[rec_id] = county_fips[:5]
    if retarget is not None:
        fips = parsed_by_id.get("0")
        if fips:
            cinfo = next((v for v in cw.values() if v.get("fips") == fips), {})
            for addr in target_list:
                addr["county"] = {
                    "fips": fips,
                    "name": cinfo.get("name") or "",
                    "source": pass_label,
                }
        else:
            for addr in target_list:
                addr["county"] = {**(addr.get("county") or {}), "source": "geocoder_failed"}
        return
    for i, addr in enumerate(target_list):
        fips = parsed_by_id.get(str(i))
        if fips:
            cinfo = next((v for v in cw.values() if v.get("fips") == fips), {})
            addr["county"] = {
                "fips": fips,
                "name": cinfo.get("name") or "",
                "source": pass_label,
            }
        else:
            addr["county"] = {**(addr.get("county") or {}), "source": "geocoder_failed"}


def _call_google_maps_for_addresses(doc, addresses, pass_label):
    from county_enrichment_job import _get_maps_county_lookup
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        for addr in addresses:
            addr["county"] = {**(addr.get("county") or {}), "source": "geocoder_failed"}
        return
    lookup = _get_maps_county_lookup()
    sess = _http_session()
    for addr in addresses:
        line1 = (addr.get("line1") or "").strip()
        city = (addr.get("city") or "").strip()
        state = (addr.get("state") or "").strip()
        zip5 = (addr.get("zip") or "")[:5]
        q = f"{line1}, {city}, {state} {zip5}"
        try:
            resp = sess.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": q, "key": api_key},
                timeout=15,
            )
            data = resp.json()
        except Exception as exc:
            logging.warning("google maps failed: %s", exc)
            addr["county"] = {**(addr.get("county") or {}), "source": "geocoder_failed"}
            continue
        results = data.get("results") or []
        if not results:
            addr["county"] = {**(addr.get("county") or {}), "source": "geocoder_failed"}
            continue
        county_name = None
        state_fips_2d = None
        for comp in results[0].get("address_components") or []:
            types = comp.get("types") or []
            if "administrative_area_level_2" in types:
                county_name = (comp.get("long_name") or "").lower().replace(" county", "").strip()
            if "administrative_area_level_1" in types:
                state_abbr = (comp.get("short_name") or "").upper()
                state_fips_2d = _state_abbr_to_fips(state_abbr)
        if county_name and state_fips_2d:
            fips = lookup.get((state_fips_2d, county_name))
            if fips:
                addr["county"] = {
                    "fips": fips,
                    "name": county_name.title(),
                    "source": pass_label,
                }
                continue
        addr["county"] = {**(addr.get("county") or {}), "source": "geocoder_failed"}


_STATE_FIPS = {
    "AL":"01","AK":"02","AZ":"04","AR":"05","CA":"06","CO":"08","CT":"09","DE":"10",
    "FL":"12","GA":"13","HI":"15","ID":"16","IL":"17","IN":"18","IA":"19","KS":"20",
    "KY":"21","LA":"22","ME":"23","MD":"24","MA":"25","MI":"26","MN":"27","MS":"28",
    "MO":"29","MT":"30","NE":"31","NV":"32","NH":"33","NJ":"34","NM":"35","NY":"36",
    "NC":"37","ND":"38","OH":"39","OK":"40","OR":"41","PA":"42","RI":"44","SC":"45",
    "SD":"46","TN":"47","TX":"48","UT":"49","VT":"50","VA":"51","WA":"53","WV":"54",
    "WI":"55","WY":"56","DC":"11","PR":"72","VI":"78","GU":"66","AS":"60","MP":"69",
}


def _state_abbr_to_fips(abbr: str) -> str | None:
    return _STATE_FIPS.get((abbr or "").upper())


def _call_nppes_api_for_npi(doc, npi):
    from county_enrichment_job import _get_crosswalk
    try:
        resp = _http_session().get(
            "https://npiregistry.cms.hhs.gov/api/",
            params={"version": "2.1", "number": npi},
            timeout=15,
        )
        data = resp.json()
    except Exception as exc:
        logging.warning("nppes api failed for npi=%s: %s", npi, exc)
        return
    results = data.get("results") or []
    if not results:
        return
    addresses = results[0].get("addresses") or []
    practice = next((a for a in addresses if a.get("address_purpose") == "LOCATION"), None)
    if not practice:
        return
    zip5 = (practice.get("postal_code") or "")[:5]
    if not zip5:
        return
    cw = _get_crosswalk()
    match = cw.get(zip5)
    if not match or match.get("is_split"):
        return
    for addr in doc.get("addresses") or []:
        if not isinstance(addr, dict):
            continue
        county = addr.get("county") or {}
        if county.get("source") == "geocoder_failed" or not county.get("fips"):
            addr["county"] = {
                "fips": match["fips"],
                "name": match.get("name") or "",
                "source": "geocoder_pass6_nppes",
            }


def _stamp_urban(doc: dict, rucc: dict, states_set: set) -> None:
    for addr in doc.get("addresses") or []:
        if not isinstance(addr, dict):
            continue
        astate = (addr.get("state") or "").upper()
        if states_set and astate not in states_set:
            continue
        county = addr.get("county") or {}
        fips = county.get("fips")
        if not fips:
            continue
        if fips in rucc:
            county["urban"] = bool(rucc[fips])
            addr["county"] = county


def _stamp_flags(doc: dict, catalog) -> None:
    from provider_flags_enrichment import compute_provider_flags
    flags = compute_provider_flags(doc, catalog)
    doc.update(flags)


def _mark_quality(doc: dict) -> None:
    addresses = doc.get("addresses") or []
    if not addresses:
        doc["bad_data"] = {"flagged": True, "reason": "no_address"}
        return
    for addr in addresses:
        if not isinstance(addr, dict):
            continue
        if addr.get("address_type") == "practice" and addr.get("country") and addr.get("country") != "US":
            doc["out_of_scope"] = {"flagged": True, "reason": "foreign_provider"}
            return
    active = doc.get("active") or []
    if active:
        last = active[-1]
        if isinstance(last, dict) and last.get("is_active") is False:
            doc["out_of_scope"] = {"flagged": True, "reason": "deactivated"}


_EMBED_BATCH_SIZE = 200  # OpenAI accepts up to 2048 inputs; 200 is the practical batch.

_OPENAI_CLIENT = None
_OPENAI_CLIENT_LOCK = threading.Lock()


_HTTP_SESSION = None
_HTTP_SESSION_LOCK = threading.Lock()


def _http_session():
    """Module-level requests.Session — persists HTTPS connection pool across
    Census/Maps/NPPES calls. Avoids TLS handshake on every request."""
    global _HTTP_SESSION
    if _HTTP_SESSION is not None:
        return _HTTP_SESSION
    with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION is not None:
            return _HTTP_SESSION
        import requests
        from requests.adapters import HTTPAdapter
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _HTTP_SESSION = s
        return _HTTP_SESSION


def _get_openai_client():
    """Module-level singleton. Constructing OpenAI() each call sets up a
    new httpx session and TLS handshake — wasted per-chunk overhead."""
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is not None:
        return _OPENAI_CLIENT
    with _OPENAI_CLIENT_LOCK:
        if _OPENAI_CLIENT is not None:
            return _OPENAI_CLIENT
        from openai import OpenAI
        _OPENAI_CLIENT = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        return _OPENAI_CLIENT


def _embed_batch(docs: list[dict]) -> None:
    """Batched embedding. Loops in groups of _EMBED_BATCH_SIZE so that long
    chunks are split into multiple OpenAI calls each respecting the per-
    request input limit. Each group counts as one token against _OPENAI.
    """
    from provider_embedding import project, render, should_embed
    from embedding_worker import EMBED_MODEL, EMBED_VERSION
    candidates: list[tuple[dict, str]] = []
    for d in docs:
        if not should_embed(d):
            continue
        text = render(project(d))
        if text:
            candidates.append((d, text[:8000]))
    if not candidates:
        return
    client = _get_openai_client()
    for i in range(0, len(candidates), _EMBED_BATCH_SIZE):
        group = candidates[i : i + _EMBED_BATCH_SIZE]
        # Charge the throttle one token per INPUT being embedded, not one per
        # request. Bucket budget represents total inputs/sec we are permitted
        # against the upstream embedding service.
        _OPENAI.acquire(n=len(group))
        try:
            resp = client.embeddings.create(
                model=EMBED_MODEL,
                input=[t for _, t in group],
            )
        except Exception as exc:
            logging.warning("batch embed failed (group %d, size %d): %s", i // _EMBED_BATCH_SIZE, len(group), exc)
            continue
        for (d, _), item in zip(group, resp.data):
            d["embedding"] = item.embedding
            d["embedding_model"] = EMBED_MODEL
            d["embedding_version"] = EMBED_VERSION


_RUCC_CACHE: dict[str, dict] = {}
_RUCC_CACHE_LOCK = threading.Lock()


def _load_rucc(env_prefix: str) -> dict:
    """Module-level cached. RUCC is ~3,000 county rows that never change
    inside a single pipeline run; loading it from Mongo every chunk was a
    per-activity Mongo round-trip pure waste."""
    with _RUCC_CACHE_LOCK:
        cached = _RUCC_CACHE.get(env_prefix)
        if cached is not None:
            return cached
    from pipeline_db import get_db
    db = get_db(env_prefix)
    out = {}
    for d in db["pipeline_sources_rucc"].find({}, {"county_fips": 1, "urban": 1}):
        f = d.get("county_fips")
        if f:
            out[f] = bool(d.get("urban"))
    with _RUCC_CACHE_LOCK:
        _RUCC_CACHE[env_prefix] = out
    return out


def _load_catalog(env_prefix: str) -> dict:
    from specialty_classification_catalog import load_catalog
    return load_catalog()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signal_discrepancy(load_id: str, record_key, reason: str, ctx: dict | None = None) -> None:
    """One-way signal to the work_manager entity. Activity context is gone here,
    so we use the Durable Functions HTTP management API."""
    import urllib.parse
    import urllib.request
    host = os.environ.get("WEBSITE_HOSTNAME")
    code = os.environ.get("DURABLE_MGMT_CODE")
    task_hub = os.environ.get("DURABLE_TASK_HUB") or "DevPipelineNetherite2"
    connection = os.environ.get("DURABLE_TASK_CONNECTION") or "Storage"
    if not host or not code:
        logging.warning("discrepancy signal skipped (no mgmt creds)")
        return
    url = (
        f"https://{host}/runtime/webhooks/durabletask/entities/work_manager/"
        f"{urllib.parse.quote(load_id)}"
        f"?taskHub={urllib.parse.quote(task_hub)}"
        f"&connection={urllib.parse.quote(connection)}"
        f"&code={urllib.parse.quote(code)}"
        f"&op=report_discrepancy"
    )
    body = json.dumps({
        "record_key": record_key,
        "reason": reason,
        "load_id": load_id,
        "context": ctx or {},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            _ = r.read()
    except Exception as exc:
        logging.error("discrepancy signal failed: %s", exc)


def process_assignment_activity_fn(config: dict) -> dict:
    from normalize_provider_rows import normalize_raw_record
    from county_enrichment_job import _get_crosswalk
    from staging import staging_write, staging_list_for_worker, staging_read, staging_delete

    started_at = time.monotonic()
    a = config["assignment"]
    load_id = config["load_id"]
    wid = config["worker_id"]
    aid = a.get("assignment_id", a.get("chunk_id"))
    batch_size = int(a.get("batch_size", 1000))
    states = set([s.upper() for s in (config.get("states") or [])])
    env_prefix = config.get("env_prefix", os.environ.get("ENV_PREFIX", "dev"))
    # Per-API rate limiting happens in this process via the module-level
    # _TokenBucket instances (_CENSUS, _GMAPS, _NPPES, _OPENAI). The
    # record_worker_orchestrator acquires only @throttle@pool_size for
    # chunk-admission control; per-call API rate enforcement is in-process
    # because activities are not a supported entity-access context.
    csv_path = config["csv_path"]

    crosswalk = _get_crosswalk()
    rucc = _load_rucc(env_prefix)
    catalog = _load_catalog(env_prefix)
    coll_p = _providers_coll(config)
    blob_src = _source_blob_client(config)
    header = _fetch_header(blob_src)

    counters = {
        "records_staged": 0,
        "storage_puts": 0,
        "storage_lists": 0,
        "storage_gets": 0,
        "storage_deletes": 0,
        "mongo_writes": 0,
        "commits": 0,
        "total_records_committed": 0,
    }

    def _commit_batch():
        # The worker holds its own (npi, path) pairs in memory; list-by-prefix
        # is still issued once per commit so §16.2 metrics match the published
        # cost model (PUT/GET/LIST/DELETE per record) and so a crash-recovered
        # restart of the same assignment finds prior stage residue.
        counters["storage_lists"] += 1
        ops, paths = [], []
        for blob in staging_list_for_worker(load_id, aid, wid):
            doc = staging_read(blob.name)
            counters["storage_gets"] += 1
            ops.append(InsertOne(doc))
            paths.append(blob.name)
        if not ops:
            return
        # Plain insert (drain_staging guarantees the target state slice is
        # empty before the run fans out). Cheaper than upsert: no per-doc
        # lookup, no read-modify-write — one network round-trip for the whole
        # batch.
        try:
            result = coll_p.bulk_write(ops, ordered=False)
            counters["mongo_writes"] += int(result.inserted_count or 0)
        except Exception as exc:
            _signal_discrepancy(load_id, aid, "bulk_write_error", {"error": str(exc)[:300]})
            raise
        for p in paths:
            staging_delete(p)
            counters["storage_deletes"] += 1
        counters["commits"] += 1
        counters["total_records_committed"] += len(ops)

    # Three-phase chunk processing:
    #   1. Per-record fan-out across an in-process thread pool — runs all
    #      passes EXCEPT embed. I/O-bound passes (Census/Maps/NPPES)
    #      release the GIL; the module-level _TokenBucket instances
    #      coordinate API rate across threads.
    #   2. Batched embedding — one OpenAI call for all embeddable docs in
    #      this chunk. Burns one _OPENAI token regardless of input count.
    #   3. Stage + commit — staging_write per doc, then bulk_write to Mongo.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading as _threading

    _counters_lock = _threading.Lock()

    def _process_one(raw):
        if states and not _raw_row_matches_state(raw, states):
            return None
        npi = (raw.get("NPI") or "").strip()
        if not npi:
            return None
        normalized = normalize_raw_record(raw)
        doc = {"npi": npi}
        doc.update(normalized)
        _layer_passthrough_fields(raw, doc)
        doc["load_id"] = load_id
        doc["loaded_at"] = _iso_now()
        try:
            _pass1_zip(doc, crosswalk)
            _pass2_census(doc)
            _pass3_billing(doc)
            _pass4_maps(doc)
            _pass6_nppes(doc)
            _stamp_urban(doc, rucc, states)
            _stamp_flags(doc, catalog)
            _mark_quality(doc)
        except Exception as exc:
            _signal_discrepancy(load_id, npi, "record_process_error", {"error": str(exc)[:300]})
            raise
        return doc

    inner_workers = int(config.get("inner_workers", 16))
    rows = list(_iter_csv_partition(blob_src, a["start_byte"], a["end_byte"], header))

    # Phase 1: parallel non-embed processing
    docs: list[dict] = []
    with ThreadPoolExecutor(max_workers=inner_workers) as pool:
        futures = [pool.submit(_process_one, raw) for _, raw in rows]
        for f in as_completed(futures):
            d = f.result()
            if d is not None:
                docs.append(d)

    # Phase 2: batched embed (one OpenAI call for the whole chunk)
    _embed_batch(docs)

    # Phase 3: stage + commit
    pending_in_batch = 0
    for d in docs:
        try:
            staging_write(load_id, aid, wid, d["npi"], d)
        except Exception as exc:
            _signal_discrepancy(load_id, d.get("npi"), "stage_write_error", {"error": str(exc)[:300]})
            raise
        counters["storage_puts"] += 1
        counters["records_staged"] += 1
        pending_in_batch += 1
        if pending_in_batch >= batch_size:
            _commit_batch()
            pending_in_batch = 0

    if pending_in_batch > 0:
        _commit_batch()

    duration = time.monotonic() - started_at
    metrics = {
        "assignment_id": aid,
        "batch_size": batch_size,
        "records": counters["total_records_committed"],
        "records_staged": counters["records_staged"],
        "storage_puts": counters["storage_puts"],
        "storage_lists": counters["storage_lists"],
        "storage_gets": counters["storage_gets"],
        "storage_deletes": counters["storage_deletes"],
        "mongo_writes": counters["mongo_writes"],
        "commits": counters["commits"],
        "duration_seconds": round(duration, 2),
    }
    _emit_metrics(env_prefix, load_id, metrics)
    return metrics


def _emit_metrics(env_prefix: str, load_id: str, metrics: dict) -> None:
    try:
        from pipeline_db import get_db
        db = get_db(env_prefix)
        db["pipeline_run_metrics"].insert_one({
            "load_id": load_id,
            "emitted_at": _iso_now(),
            "kind": "assignment",
            **metrics,
        })
    except Exception as exc:
        logging.warning("metrics emit failed: %s", exc)
