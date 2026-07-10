# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Ensure provider collection indexes exist after the pipeline cluster is awake."""

from __future__ import annotations

import logging
import time

from pymongo import MongoClient

_log = logging.getLogger("ensure_provider_indexes")


def _wait_for_cluster_ready(client: MongoClient, wait_minutes: int) -> None:
    deadline = time.monotonic() + wait_minutes * 60
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            client.admin.command("ping", maxTimeMS=5000)
            _log.info("cluster ready after %d attempt(s) (~%.0fs)", attempt, attempt * 5)
            return
        except Exception as exc:
            _log.info("cluster not ready attempt=%d: %s", attempt, exc)
            time.sleep(5)
    raise TimeoutError(f"cluster not ready after {wait_minutes} minutes")


def _ensure_indexes(collection, specs: list[tuple[list, dict]]) -> list[dict]:
    out = []
    for keys, kwargs in specs:
        started = time.monotonic()
        name = kwargs.get("name") or "_".join(f"{k[0]}_{k[1]}" for k in keys)
        before = list(collection.list_indexes())
        exists = any(idx.get("name") == name for idx in before)
        if not exists:
            collection.create_index(keys, **kwargs)
        out.append({
            "name": name,
            "already": exists,
            "seconds": round(time.monotonic() - started, 1),
        })
        _log.info("ensure_provider_indexes: %s (already=%s) in %.1fs", name, exists, out[-1]["seconds"])
    return out


def ensure_provider_indexes_fn(config: dict) -> dict:
    from pipeline_db import get_mongo

    provider_collection = config.get("provider_collection", "dev_PublicHealthData.providers_v1")
    if "." not in provider_collection:
        provider_collection = f"dev_PublicHealthData.{provider_collection}"
    db_name, coll_name = provider_collection.split(".", 1)
    cluster_wait_minutes = int(config.get("cluster_wait_minutes", 20))
    client = get_mongo()
    _wait_for_cluster_ready(client, cluster_wait_minutes)
    coll = client[db_name][coll_name]
    indexes = _ensure_indexes(coll, [
        ([("npi", 1)], {"name": "npi_1", "unique": True}),
        ([("addresses.county.source", 1)], {"name": "addresses.county.source_1"}),
        ([("addresses.address_type", 1), ("addresses.state", 1)], {"name": "addresses.address_type_1_addresses.state_1"}),
    ])
    return {"provider_collection": provider_collection, "indexes": indexes}
