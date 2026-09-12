"""Smoke test for the gateway's MongoClusterMigrator route.

Scope: assert the pipeline gateway was contacted and accepted a
MongoClusterMigrator request with a well-formed response.

Out of scope (handled elsewhere):
  - chain health / runbook failure detection: belongs to /Status
    infrastructure (walks the AA job chain) and to process monitoring.
  - data-motion verification (counts, indexes, reconciliation): the
    operator inspects via /Status once /Status reports chain success.

Payload: business-addressed providers in WY+VT from the pipeline
cluster's PipelinePublicHealthData.providers to the frontend cluster's
PublicHealthData.provider_v02, partitioned per state.
"""
import json
import os
from pathlib import Path

import pytest
import requests
import sys as _sys, pathlib as _pl

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "ChatHealthyLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / '.git').exists():
        _lib = _d / 'ChatHealthyLib' / 'src'
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException


_GATEWAY_BASE = "https://chathealthydatapipelinesgatewayfunctionapp.azurewebsites.net/api"

_PAYLOAD = {
    "ChatHealthyTask": "MongoClusterMigrator",
    "source_cluster": "pipeline",
    "source_database": "PipelinePublicHealthData",
    "source_collection": "providers",
    "destination_cluster": "frontend",
    "destination_database": "PublicHealthData",
    "destination_collection": "provider_v02",
    "filter": {
        "business_address.state": {"$in": ["WY", "VT"]},
    },
    "thread_criteria": {
        "business_address.state": "*",
    },
    "preserve_indices": True,
    "reservation_duration_minutes": 300,
}


def _load_env():
    env_file = Path(__file__).resolve().parents[3] / ".env"
    if not env_file.is_file():
        pytest.skip(f".env not found at {env_file}")
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
    raise ChatHealthyException(
        mode="configuration_missing",
        component="mongo_cluster_migrator_business_wy_vt_test",
        message="no 'skip' token found in API_TOKEN_MAP")


def test_gateway_accepts_mongo_cluster_migrator_request():
    _load_env()
    token = _bearer_token()

    _CH_LOG.info(f"[fire] POST {_GATEWAY_BASE}/Router")
    _CH_LOG.info(f"[fire] payload={json.dumps(_PAYLOAD)}")

    r = requests.post(
        f"{_GATEWAY_BASE}/Router",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=_PAYLOAD,
        timeout=60,
    )

    # Report the full gateway response BEFORE asserting, so the operator
    # sees exactly what came back regardless of whether the test passes.
    try:
        body_pretty = json.dumps(r.json(), indent=2)
    except ValueError:
        body_pretty = r.text[:1000]
    _CH_LOG.info(f"[gateway] status_code={r.status_code}")
    _CH_LOG.info(f"[gateway] response_body=\n{body_pretty}")

    assert r.status_code == 202, f"/Router returned {r.status_code}: {r.text[:500]}"
    body = r.json()
    assert body.get("success") is True, f"/Router success != True: {body}"
    assert body.get("task") == "MongoClusterMigrator", \
        f"/Router task != MongoClusterMigrator: {body}"
    job_id = body.get("job_id")
    assert isinstance(job_id, str) and len(job_id) == 32, \
        f"bad job_id: {job_id!r}"
    request_guid = body.get("request_guid")
    assert isinstance(request_guid, str) and len(request_guid) > 0, \
        f"bad request_guid: {request_guid!r}"
