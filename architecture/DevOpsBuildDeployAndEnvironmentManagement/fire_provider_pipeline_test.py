# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Fire the ProviderPipelineRunbook webhook against the SMOKE_TEST_ENV env.

Runs as part of deploy_chathealthy.py --tests fire_provider_pipeline so
firing a pipeline test is authorized through the same deploy-chain gate
as everything else, not through a separate oneoff wrapper.

Payload defaults: state_scope=[VT,DE], load_mode=full, invocation_mode=
tests. Overrides via env: PIPELINE_TEST_STATE_SCOPE (comma-separated),
PIPELINE_TEST_LOAD_MODE.
"""
from __future__ import annotations

import json
import os
import urllib.request

import pytest
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

_KV_BY_ENV = {
    "dev": "https://kv-chpipeline-dev.vault.azure.net/",
    "qa": "https://kv-chpipeline-qa.vault.azure.net/",
    "prod": "https://kv-chpipeline-prod.vault.azure.net/",
}
_WEBHOOK_SECRET_NAME = "PROVIDER-PIPELINE-WEBHOOK-URL"


def test_fire_provider_pipeline() -> None:
    env = os.environ.get("SMOKE_TEST_ENV", "dev").strip().lower()
    kv_uri = _KV_BY_ENV.get(env)
    if not kv_uri:
        pytest.skip(f"no KV URI for env={env!r}")

    kv = SecretClient(vault_url=kv_uri, credential=DefaultAzureCredential())
    url = kv.get_secret(_WEBHOOK_SECRET_NAME).value

    state_scope_raw = os.environ.get("PIPELINE_TEST_STATE_SCOPE", "VT,DE")
    state_scope = [s.strip().upper() for s in state_scope_raw.split(",") if s.strip()]
    load_mode = os.environ.get("PIPELINE_TEST_LOAD_MODE", "full")
    payload = {
        "state_scope": state_scope,
        "load_mode": load_mode,
        "invocation_mode": "tests",
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, method="POST", data=body,
        headers={"Content-Type": "application/json"},
    )
    print(f"[fire_provider_pipeline] POST env={env} payload={payload}")
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.status == 202, f"expected HTTP 202, got {r.status}"
        resp = r.read().decode("utf-8")
        print(f"[fire_provider_pipeline] HTTP 202 response={resp}")
