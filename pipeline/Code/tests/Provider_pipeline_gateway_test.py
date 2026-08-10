"""End-to-end gateway tests.

Test order is intentional: the contract-violation test runs first to prove
the gateway rejects an incomplete payload with 400 BEFORE we burn cluster
time on a real orchestration. Only then does the happy-path test fire a
real ProviderPipeline against Wyoming, writing to
dev_PublicHealthData.functionAppTest so the live providers collection
(6.1M records) is untouched.
"""
from __future__ import annotations

from pathlib import Path

import requests

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "FrontEndApplicationLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()

GATEWAY_URL = (
    "https://chathealthydatapipelinesgatewayfunctionapp.azurewebsites.net"
    "/api/Router"
)

REPO_ROOT = Path(__file__).resolve().parents[3]
HTTP_FILE = REPO_ROOT / "pipeline.http"


def _bearer_token() -> str:
    raw = HTTP_FILE.read_text(encoding="utf-8").strip()
    if raw.startswith("Bearer "):
        raw = raw[len("Bearer "):].strip()
    return raw


def _post(body: dict, timeout: int = 180) -> requests.Response:
    token = _bearer_token()
    return requests.post(
        GATEWAY_URL,
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        timeout=timeout,
    )


def test_a_missing_required_arg_returns_400() -> None:
    """ProviderPipeline contract requires `expected_duration_minutes`. The
    gateway MUST reject the request with 400 + a clear missing-args message
    BEFORE forwarding to the durable. No Durable instance, no reservation,
    no Mongo writes — just the gateway saying no."""
    resp = _post({
        "ChatHealthyTask":     "ProviderPipeline",
        "states":              ["WY"],
        "provider_collection": "dev_PublicHealthData.functionAppTest",
        # expected_duration_minutes intentionally omitted
    })
    assert resp.status_code == 400, (
        f"Expected 400 from contract check, got {resp.status_code}: "
        f"{resp.text[:500]}"
    )
    data = resp.json()
    assert data.get("success") is False, data
    error = data.get("error", "")
    assert "expected_duration_minutes" in error, (
        f"Error should name the missing arg; got: {error!r}"
    )


def test_b_full_payload_returns_202() -> None:
    """Same call with the missing arg supplied. Gateway validates, forwards,
    durable Router replies 202 with the instance id. Orchestration writes
    to dev_PublicHealthData.functionAppTest."""
    resp = _post({
        "ChatHealthyTask":           "ProviderPipeline",
        "states":                    ["WY"],
        "provider_collection":       "dev_PublicHealthData.functionAppTest",
        "expected_duration_minutes": 30,
    })
    assert resp.status_code == 202, (
        f"Gateway returned {resp.status_code}: {resp.text[:500]}"
    )
    data = resp.json()
    instance_id = data.get("id")
    assert instance_id, f"202 response missing 'id': {data}"
    _CH_LOG.info(f"\nJOB ID: {instance_id}")
