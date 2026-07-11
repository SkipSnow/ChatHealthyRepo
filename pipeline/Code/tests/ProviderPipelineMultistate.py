"""POST the Function App's Router for ProviderPipeline across 8 states and assert 202.

States: MA, MD, ME, MI, MN, MO, MS, MT.

Repeatable invocation of the exact same HTTP request a hand-run of
`pipeline_trigger.py ProviderPipeline --payload-json
'{"states":["MA","MD","ME","MI","MN","MO","MS","MT"],"pipeline_cluster":"ChatHealthyDataPipelines"}'`
produces.

The Durable instance id (Job ID) returned in the 202 response is printed to
stdout so it shows in `pytest -s` output (or any -rA summary).
"""
from __future__ import annotations

from pathlib import Path

import requests

ROUTER_URL = (
    "https://dev-pipeline-records-as-messages.yellowbay-d50afa88."
    "eastus2.azurecontainerapps.io/api/Router"
)

REPO_ROOT = Path(__file__).resolve().parents[3]
HTTP_FILE = REPO_ROOT / "pipeline.http"

BODY = {
    "ChatHealthyTask": "ProviderPipeline",
    "payload": {
        "states": ["MA", "MD", "ME", "MI", "MN", "MO", "MS", "MT"],
        "pipeline_cluster": "ChatHealthyDataPipelines",
    },
}


def _bearer_token() -> str:
    raw = HTTP_FILE.read_text(encoding="utf-8").strip()
    if raw.startswith("Bearer "):
        raw = raw[len("Bearer "):].strip()
    return raw


def test_provider_pipeline_multistate_returns_202() -> None:
    token = _bearer_token()
    resp = requests.post(
        ROUTER_URL,
        json=BODY,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=180,
    )
    assert resp.status_code == 202, (
        f"Router returned {resp.status_code}: {resp.text[:500]}"
    )
    data = resp.json()
    instance_id = data.get("id")
    assert instance_id, f"202 response missing 'id': {data}"
    print(f"\nJOB ID: {instance_id}")
