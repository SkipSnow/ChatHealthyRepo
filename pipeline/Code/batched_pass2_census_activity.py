# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""batched_pass2_census_activity - post-ingest batched Census enrichment.

The per-record ingest in process_assignment_activity does pass1 only
(zip->county crosswalk, local). Practice addresses pass1 cannot resolve
exit ingest with county.fips=null. This activity collapses what used to
be one Census HTTP per record into one Census HTTP per chunk of unique
addresses, using the addressbatch endpoint the way it is designed.

Algorithm:
  1. Aggregate cohort: providers with at least one practice address
     where county.fips is missing/null.
  2. De-duplicate the cohort addresses across providers (the same office
     shared by many providers becomes one lookup).
  3. Chunk unique addresses into batches of census_batch_size.
  4. Fire each chunk to Census via the existing
     process_assignment_activity._call_census_for_addresses helper.
     Up to census_max_concurrent chunks in flight at once.
  5. bulk_write per provider, setting addresses[i].county for each
     resolved practice address (with urban stamped from rucc) and
     setting addresses[i].county.source="geocoder_pass2_failed" for
     addresses Census could not resolve.

Addresses that exit this activity with source=geocoder_pass2_failed are
picked up by the existing recovery_worker_orchestrator, which runs the
per-record pass3 -> pass4 -> pass6 chain over them.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from pymongo import MongoClient, UpdateOne


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _split_collection(fqn: str | None) -> tuple[str, str]:
    if not fqn:
        fqn = f"{os.environ.get('ENV_PREFIX', 'dev')}_PublicHealthData.providers"
    db, coll = fqn.split(".", 1)
    return db, coll


def _addr_key(addr: dict) -> str:
    line1 = (addr.get("line1") or "").strip().upper()
    city = (addr.get("city") or "").strip().upper()
    state = (addr.get("state") or "").strip().upper()
    zip5 = (addr.get("zip") or "")[:5]
    return f"{line1}|{city}|{state}|{zip5}"


def batched_pass2_census_fn(config: dict) -> dict:
    t0 = time.time()
    load_id = config.get("load_id", "")
    batch_size = int(config.get("census_batch_size", 1000))
    max_concurrent = int(config.get("census_max_concurrent", 4))
    env_prefix = config.get("env_prefix", os.environ.get("ENV_PREFIX", "dev"))
    blob_container = config.get("blob_container", "provider-data")

    db_name, coll_name = _split_collection(config.get("provider_collection"))
    client = MongoClient(
        os.environ["MONGO_connectionString"],
        serverSelectionTimeoutMS=120_000,
        maxPoolSize=64,
    )
    coll = client[db_name][coll_name]

    from process_assignment_activity import (
        _call_census_for_addresses,
        _load_rucc,
    )

    rucc = _load_rucc(env_prefix, blob_container)

    cursor = coll.find(
        {"addresses": {"$elemMatch": {
            "address_type": "practice",
            "county.fips": None,
        }}},
        projection={"_id": 0, "npi": 1, "addresses": 1},
        no_cursor_timeout=True,
    )

    unique_addrs: dict[str, dict] = {}
    addr_users: dict[str, list[tuple[str, int]]] = {}

    n_providers_in_cohort = 0
    try:
        for d in cursor:
            npi = str(d.get("npi") or "")
            if not npi:
                continue
            n_providers_in_cohort += 1
            for i, addr in enumerate(d.get("addresses") or []):
                if not isinstance(addr, dict):
                    continue
                if addr.get("address_type") != "practice":
                    continue
                county = addr.get("county") or {}
                if county.get("fips"):
                    continue
                if not (addr.get("line1") or "").strip():
                    continue
                if not (addr.get("zip") or "")[:5]:
                    continue
                k = _addr_key(addr)
                if k not in unique_addrs:
                    unique_addrs[k] = addr
                addr_users.setdefault(k, []).append((npi, i))
    finally:
        cursor.close()

    keys = list(unique_addrs.keys())
    n_unique = len(keys)
    logging.info(
        "batched_pass2_census: load_id=%s cohort=%d providers, "
        "unique_addrs=%d, batch_size=%d, max_concurrent=%d",
        load_id, n_providers_in_cohort, n_unique, batch_size, max_concurrent,
    )

    if n_unique == 0:
        return {
            "providers_in_cohort": n_providers_in_cohort,
            "unique_addresses": 0,
            "batches_posted": 0,
            "batches_failed": 0,
            "resolved": 0,
            "failed": 0,
            "providers_modified": 0,
            "duration_seconds": round(time.time() - t0, 2),
        }

    chunks: list[list[str]] = [keys[i:i + batch_size] for i in range(0, len(keys), batch_size)]

    resolved_by_key: dict[str, dict] = {}
    failed_keys: list[str] = []
    batches_posted = 0
    batches_failed = 0

    def _resolve_chunk(chunk_keys: list[str]) -> tuple[dict, list, bool]:
        addrs = [dict(unique_addrs[k]) for k in chunk_keys]
        try:
            _call_census_for_addresses(
                {},
                addrs,
                pass_label="geocoder_pass2_batch",
                failure_label="geocoder_pass2_failed",
            )
        except Exception as exc:
            logging.error("batched_pass2_census: census POST raised: %s", exc)
            return {}, list(chunk_keys), False
        ok: dict = {}
        bad: list = []
        for k, a in zip(chunk_keys, addrs):
            c = a.get("county") or {}
            if c.get("fips"):
                ok[k] = {
                    "fips": c["fips"],
                    "name": c.get("name") or "",
                    "source": c.get("source") or "geocoder_pass2_batch",
                }
            else:
                bad.append(k)
        return ok, bad, True

    with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        futures = [pool.submit(_resolve_chunk, chunk) for chunk in chunks]
        for f in as_completed(futures):
            ok, bad, success = f.result()
            resolved_by_key.update(ok)
            failed_keys.extend(bad)
            if success:
                batches_posted += 1
            else:
                batches_failed += 1

    per_provider_writes: dict[str, list[tuple[int, dict]]] = {}
    for k, county in resolved_by_key.items():
        for npi, idx in addr_users.get(k, []):
            per_provider_writes.setdefault(npi, []).append((idx, county))
    for k in failed_keys:
        for npi, idx in addr_users.get(k, []):
            per_provider_writes.setdefault(npi, []).append(
                (idx, {"source": "geocoder_pass2_failed"})
            )

    def _county_with_urban(county: dict) -> dict:
        out = dict(county)
        fips = out.get("fips")
        if fips and fips in rucc:
            out["urban"] = bool(rucc[fips])
        return out

    ops: list[UpdateOne] = []
    for npi, updates in per_provider_writes.items():
        set_doc: dict = {}
        for idx, county in updates:
            if county.get("fips"):
                set_doc[f"addresses.{idx}.county"] = _county_with_urban(county)
            else:
                set_doc[f"addresses.{idx}.county.source"] = county["source"]
        if set_doc:
            ops.append(UpdateOne({"npi": npi}, {"$set": set_doc}))

    n_modified = 0
    BULK_CHUNK = 1000
    for i in range(0, len(ops), BULK_CHUNK):
        sub = ops[i:i + BULK_CHUNK]
        if not sub:
            continue
        try:
            r = coll.bulk_write(sub, ordered=False)
            n_modified += int(r.modified_count or 0)
        except Exception as exc:
            logging.error("batched_pass2_census: bulk_write failed at sub %d: %s", i, exc)
            raise

    return {
        "providers_in_cohort": n_providers_in_cohort,
        "unique_addresses": n_unique,
        "batches_posted": batches_posted,
        "batches_failed": batches_failed,
        "resolved": len(resolved_by_key),
        "failed": len(failed_keys),
        "providers_modified": n_modified,
        "duration_seconds": round(time.time() - t0, 2),
    }
