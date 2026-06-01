"""POST the Gateway FA's /api/Router for ProviderPipeline against Wyoming
and assert 202.

Fires through the new ChatHealthyDataPipelinesGatewayFunctionApp (the pure
HTTP facade) — NOT the upstream Durable Router directly. Writes provider
records to dev_PublicHealthData.functionAppTest so the live providers
collection (dev_PublicHealthData.providers, 6.1M records) is untouched.

The Durable instance id (Job ID) returned in the 202 response is printed to
stdout so it shows in `pytest -s` output (or any -rA summary).
"""
from __future__ import annotations

from pathlib import Path

import requests

GATEWAY_URL = (
    "https://chathealthydatapipelinesgatewayfunctionapp.azurewebsites.net"
    "/api/Router"
)

REPO_ROOT = Path(__file__).resolve().parents[3]
HTTP_FILE = REPO_ROOT / "pipeline.http"

BODY = {
    "ChatHealthyTask": "ProviderPipeline",
    "states": ["WY"],
    "provider_collection": "dev_PublicHealthData.functionAppTest",
    "expected_duration_minutes": 30,
}


def _bearer_token() -> str:
    raw = HTTP_FILE.read_text(encoding="utf-8").strip()
    if raw.startswith("Bearer "):
        raw = raw[len("Bearer "):].strip()
    return raw


def test_gateway_provider_pipeline_wy_returns_202() -> None:
    token = _bearer_token()
    resp = requests.post(
        GATEWAY_URL,
        json=BODY,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=180,
    )
    assert resp.status_code == 202, (
        f"Gateway returned {resp.status_code}: {resp.text[:500]}"
    )
    data = resp.json()
    instance_id = data.get("id")
    assert instance_id, f"202 response missing 'id': {data}"
    print(f"\nJOB ID: {instance_id}")
