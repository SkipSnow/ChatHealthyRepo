"""End-to-end smoke test for MongoClusterMigrator (CHDM).

Posts the agreed migration payload to the live pipeline gateway, polls
/Status until ended_at is set, and asserts the run completed cleanly
with reconciliation and indexes mirrored.

Source: pipeline cluster, dev_PublicHealthData.providers (~6.1M docs).
Destination: frontend cluster, PublicHealthData.provider_v02 (created
by the migrator). Job filter narrows the migration to providers whose
addresses[] has a business-typed element in WY or VT (~25,573 docs at
inspection time on 2026-06-02). thread_criteria partitions per state,
scoped to business addresses, producing 2 threads (VT + WY).
"""
import json
import os
import time
from pathlib import Path

import pytest
import requests
from pymongo import MongoClient


_GATEWAY_BASE = "https://chathealthydatapipelinesgatewayfunctionapp.azurewebsites.net/api"
_POLL_INTERVAL_SEC = 30
_POLL_TIMEOUT_SEC = 60 * 60

_PAYLOAD = {
    "ChatHealthyTask": "MongoClusterMigrator",
    "source_cluster": "pipeline",
    "source_database": "dev_PublicHealthData",
    "source_collection": "providers",
    "destination_cluster": "frontend",
    "destination_database": "PublicHealthData",
    "destination_collection": "provider_v02",
    "filter": {
        "addresses.address_type": "business",
        "addresses.state": {"$in": ["WY", "VT"]},
    },
    "thread_criteria": {
        "addresses.address_type": "business",
        "addresses.state": "*",
    },
    "preserve_indices": True,
    "reservation_duration_minutes": 240,
}


def _load_env():
    env_file = Path(__file__).resolve().parents[3] / "Code" / ".env"
    if not env_file.is_file():
        pytest.skip(f"Code/.env not found at {env_file}")
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _bearer_token() -> str:
    raw = os.environ.get("API_TOKEN_MAP", "{}")
    token_map = json.loads(raw)
    for token, user in token_map.items():
        if user == "skip":
            return token
    raise RuntimeError("no 'skip' token found in API_TOKEN_MAP")


def _fire_router(token: str) -> dict:
    r = requests.post(
        f"{_GATEWAY_BASE}/Router",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=_PAYLOAD,
        timeout=60,
    )
    assert r.status_code == 202, f"/Router returned {r.status_code}: {r.text[:500]}"
    body = r.json()
    assert body.get("success") is True, f"/Router success != True: {body}"
    job_id = body.get("job_id")
    assert isinstance(job_id, str) and len(job_id) == 32, f"bad job_id: {job_id!r}"
    return body


def _poll_status(token: str, job_id: str) -> dict:
    deadline = time.time() + _POLL_TIMEOUT_SEC
    last_status = None
    while time.time() < deadline:
        r = requests.post(
            f"{_GATEWAY_BASE}/Status",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"job_id": job_id},
            timeout=30,
        )
        if r.status_code == 404:
            print(f"[poll] job_id={job_id} status doc not yet created", flush=True)
            time.sleep(_POLL_INTERVAL_SEC)
            continue
        assert r.status_code == 200, f"/Status returned {r.status_code}: {r.text[:500]}"
        body = r.json()
        status = body.get("status", {})
        last_status = status
        ended_at = status.get("ended_at")
        threads = status.get("threads", [])
        thread_lines = ", ".join(
            f"t{t.get('thread_id')}={t.get('source_read_count', 0)}r/"
            f"{t.get('dest_write_count', 0)}w/{t.get('status', '-')}"
            for t in threads
        )
        print(f"[poll] job_id={job_id} ended_at={ended_at} threads=[{thread_lines}]", flush=True)
        if ended_at is not None:
            return status
        time.sleep(_POLL_INTERVAL_SEC)
    pytest.fail(
        f"job {job_id} did not reach ended_at within {_POLL_TIMEOUT_SEC}s; "
        f"last status={last_status}"
    )


def _user_index_names(uri: str, db_name: str, coll_name: str) -> set[str]:
    client = MongoClient(uri)
    coll = client[db_name][coll_name]
    return {idx.get("name") for idx in coll.list_indexes() if idx.get("name") != "_id_"}


def _composed_jobfilter_for_count() -> dict:
    """Same Mongo query the migrator composes internally from the dotted-
    path JobFilter the operator supplies. Used for the test's independent
    count_documents call so the reconciliation assertion is exact."""
    return {
        "addresses": {
            "$elemMatch": {
                "address_type": "business",
                "state": {"$in": ["WY", "VT"]},
            }
        }
    }


def test_chdm_business_wy_vt_migration():
    _load_env()
    token = _bearer_token()

    print(f"[fire] POST /Router payload={json.dumps(_PAYLOAD)}", flush=True)
    fire_response = _fire_router(token)
    job_id = fire_response["job_id"]
    orchestrator_aa = fire_response.get("orchestrator_aa_job_id")
    request_guid = fire_response.get("request_guid")
    print(
        f"[fire] job_id={job_id} request_guid={request_guid} "
        f"orchestrator_aa={orchestrator_aa}",
        flush=True,
    )

    status = _poll_status(token, job_id)

    assert status.get("has_exception") is False, \
        f"run ended with has_exception=True: {status}"

    threads = status.get("threads", [])
    assert len(threads) == 2, \
        f"expected 2 threads (one per state), got {len(threads)}: {threads}"
    for t in threads:
        assert t.get("status") == "ok", \
            f"thread {t.get('thread_id')} status != ok: {t}"

    total_writes = sum(int(t.get("dest_write_count", 0)) for t in threads)
    src_uri = os.environ["MONGO_CLUSTER_pipeline_connectionString"]
    dst_uri = os.environ["MONGO_CLUSTER_frontend_connectionString"]
    src_count = MongoClient(src_uri)[_PAYLOAD["source_database"]][
        _PAYLOAD["source_collection"]
    ].count_documents(_composed_jobfilter_for_count())
    assert total_writes == src_count, \
        f"reconciliation mismatch: writes={total_writes} source={src_count}"

    dest_count = MongoClient(dst_uri)[_PAYLOAD["destination_database"]][
        _PAYLOAD["destination_collection"]
    ].count_documents({})
    assert dest_count == total_writes, \
        f"destination count={dest_count} != threaded writes={total_writes}"

    src_idx = _user_index_names(
        src_uri, _PAYLOAD["source_database"], _PAYLOAD["source_collection"],
    )
    dst_idx = _user_index_names(
        dst_uri, _PAYLOAD["destination_database"], _PAYLOAD["destination_collection"],
    )
    missing = src_idx - dst_idx
    assert not missing, f"destination missing user-defined indexes: {missing}"

    print(
        f"[done] job_id={job_id} writes={total_writes} source={src_count} "
        f"dest={dest_count} indexes_mirrored={len(src_idx)}",
        flush=True,
    )
