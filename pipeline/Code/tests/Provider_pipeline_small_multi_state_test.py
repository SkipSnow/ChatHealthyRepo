"""POST the Function App's Router for ProviderPipeline across the 33 remaining
US states (everything not covered by the other two multi-state test files) and
assert 202.

States: AK, AL, AR, AZ, CO, CT, DC, DE, HI, IA, ID, IN, KS, KY, LA, ND, NE, NH,
NM, NV, OK, OR, RI, SC, SD, TN, UT, VA, VT, WA, WI, WV, WY.

Combined with the prior two tests this covers all 50 states + DC:
  ProviderPipelineMultistate.py            : MA, MD, ME, MI, MN, MO, MS, MT (8)
  Provider_pipeline_large_multi_state_test : CA, TX, FL, NY, PA, IL, OH, GA, NC, NJ (10)
  this file                                : the remaining 33

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
        "states": [
            "AK", "AL", "AR", "AZ", "CO", "CT", "DC", "DE", "HI", "IA",
            "ID", "IN", "KS", "KY", "LA", "ND", "NE", "NH", "NM", "NV",
            "OK", "OR", "RI", "SC", "SD", "TN", "UT", "VA", "VT", "WA",
            "WI", "WV", "WY",
        ],
        "pipeline_cluster": "ChatHealthyDataPipelines",
        "expected_duration_minutes": 1440,
    },
}


def _bearer_token() -> str:
    raw = HTTP_FILE.read_text(encoding="utf-8").strip()
    if raw.startswith("Bearer "):
        raw = raw[len("Bearer "):].strip()
    return raw


def test_provider_pipeline_remaining_states_returns_202() -> None:
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
