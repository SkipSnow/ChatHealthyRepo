# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""process_assignment_activity - the straight-through per-record worker.

For one assignment (byte range of the NPPES CSV), inline chain:
  stream -> state-filter -> normalize -> pass1 zip -> pass2 census ->
  pass3 billing -> pass4 maps -> pass6 nppes -> stamp_urban -> stamp_flags ->
  mark_quality -> should_embed? -> embed -> stage (one blob per record).

At assignment close: list-by-prefix, read each, bulk_write upsert, delete blobs.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from datetime import datetime, timezone

from pymongo import MongoClient, UpdateOne


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

def _fetch_header(blob_client) -> list:
    header_bytes = blob_client.download_blob(offset=0, length=32768).readall()
    header_text = header_bytes.decode("utf-8", errors="replace")
    if "\n" not in header_text:
        raise RuntimeError("Header line exceeds 32KB - unexpected CSV format.")
    header_line = header_text.split("\n")[0]
    return list(csv.reader([header_line]))[0]


def _iter_csv_partition(blob_client, start_byte: int, end_byte: int, header: list):
    stream = blob_client.download_blob(offset=start_byte, length=end_byte - start_byte)
    local_id = 0
    buffer = b""
    expected_field_count = len(header)
    for chunk in stream.chunks():
        buffer += chunk
        while b"\n" in buffer:
            line_bytes, buffer = buffer.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace")
            if not line:
                continue
            row = next(csv.reader([line]), None)
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


def _pass2_census(doc: dict, throttle_callback) -> None:
    targets = _addresses_needing_geocoding(doc, addr_type="practice")
    if not targets:
        return
    if throttle_callback:
        throttle_callback("census", n=len(targets))
    _call_census_for_addresses(doc, targets, pass_label="geocoder_pass2_batch")


def _pass3_billing(doc: dict, throttle_callback) -> None:
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
    billing_addr = None
    for addr in doc.get("addresses") or []:
        if isinstance(addr, dict) and addr.get("address_type") == "business":
            billing_addr = addr
            break
    if not billing_addr:
        return
    if throttle_callback:
        throttle_callback("census", n=1)
    _call_census_for_addresses(doc, [billing_addr], pass_label="geocoder_pass3_billing", retarget=targets)


def _pass4_maps(doc: dict, throttle_callback) -> None:
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
    if throttle_callback:
        throttle_callback("google_maps", n=len(targets))
    _call_google_maps_for_addresses(doc, targets, pass_label="geocoder_pass4_maps")


def _pass6_nppes(doc: dict, throttle_callback) -> None:
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
    if throttle_callback:
        throttle_callback("nppes", n=1)
    _call_nppes_api_for_npi(doc, npi)


def _call_census_for_addresses(doc, addresses, pass_label, retarget=None):
    from county_enrichment_job import _get_crosswalk
    import requests
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
        resp = requests.post(url, files=files, data=data, timeout=180)
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
    import requests
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        for addr in addresses:
            addr["county"] = {**(addr.get("county") or {}), "source": "geocoder_failed"}
        return
    lookup = _get_maps_county_lookup()
    for addr in addresses:
        line1 = (addr.get("line1") or "").strip()
        city = (addr.get("city") or "").strip()
        state = (addr.get("state") or "").strip()
        zip5 = (addr.get("zip") or "")[:5]
        q = f"{line1}, {city}, {state} {zip5}"
        try:
            resp = requests.get(
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
    import requests
    from county_enrichment_job import _get_crosswalk
    try:
        resp = requests.get(
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


def _embed(doc: dict, throttle_callback) -> None:
    from provider_embedding import project, render
    from embedding_worker import EMBED_MODEL, EMBED_VERSION
    text = render(project(doc))
    if not text:
        return
    if throttle_callback:
        throttle_callback("openai", n=1)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        resp = client.embeddings.create(model=EMBED_MODEL, input=text[:8000])
        vector = resp.data[0].embedding
    except Exception as exc:
        logging.warning("embed failed: %s", exc)
        return
    doc["embedding"] = vector
    doc["embedding_model"] = EMBED_MODEL
    doc["embedding_version"] = EMBED_VERSION


def _load_rucc(env_prefix: str) -> dict:
    from pipeline_db import get_db
    db = get_db(env_prefix)
    out = {}
    for d in db["pipeline_sources_rucc"].find({}, {"county_fips": 1, "urban": 1}):
        f = d.get("county_fips")
        if f:
            out[f] = bool(d.get("urban"))
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
    batch_size = int(a.get("batch_size", 500))
    states = set([s.upper() for s in (config.get("states") or [])])
    env_prefix = config.get("env_prefix", os.environ.get("ENV_PREFIX", "dev"))
    # Throttle permits for this assignment were acquired by the parent
    # record_worker_orchestrator via call_entity before this activity was
    # scheduled. The activity makes API calls freely within that budget;
    # no per-call entity round-trip from the activity (which Microsoft does
    # not document as a supported entity-access context).
    throttle_callback = None
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
            ops.append(UpdateOne({"npi": doc["npi"]}, {"$set": doc}, upsert=True))
            paths.append(blob.name)
        if not ops:
            return
        try:
            result = coll_p.bulk_write(ops, ordered=False)
            counters["mongo_writes"] += (
                (result.upserted_count or 0)
                + (result.modified_count or 0)
                + (result.matched_count or 0)
            )
        except Exception as exc:
            _signal_discrepancy(load_id, aid, "bulk_write_error", {"error": str(exc)[:300]})
            raise
        for p in paths:
            staging_delete(p)
            counters["storage_deletes"] += 1
        counters["commits"] += 1
        counters["total_records_committed"] += len(ops)

    pending_in_batch = 0
    for rid, raw in _iter_csv_partition(blob_src, a["start_byte"], a["end_byte"], header):
        if states and not _raw_row_matches_state(raw, states):
            continue
        npi = (raw.get("NPI") or "").strip()
        if not npi:
            continue
        normalized = normalize_raw_record(raw)
        doc = {"npi": npi}
        doc.update(normalized)
        _layer_passthrough_fields(raw, doc)
        doc["load_id"] = load_id
        doc["loaded_at"] = _iso_now()

        try:
            _pass1_zip(doc, crosswalk)
            _pass2_census(doc, throttle_callback)
            _pass3_billing(doc, throttle_callback)
            _pass4_maps(doc, throttle_callback)
            _pass6_nppes(doc, throttle_callback)
            _stamp_urban(doc, rucc, states)
            _stamp_flags(doc, catalog)
            _mark_quality(doc)
            from provider_embedding import should_embed
            if should_embed(doc):
                _embed(doc, throttle_callback)
            staging_write(load_id, aid, wid, npi, doc)
            counters["storage_puts"] += 1
            counters["records_staged"] += 1
            pending_in_batch += 1
        except Exception as exc:
            _signal_discrepancy(load_id, npi, "record_process_error", {"error": str(exc)[:300]})
            raise

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
