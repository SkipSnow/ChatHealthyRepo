# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Fire the ProviderPipelineRunbook webhook against the SMOKE_TEST_ENV env.

Runs as part of deploy_chathealthy.py --tests fire_provider_pipeline so
firing a pipeline test is authorized through the same deploy-chain gate
as everything else, not through a separate oneoff wrapper.

Reads the webhook URL from Key Vault via the ambient `az` CLI session
(no azure-identity SDK dependency).

Payload defaults: state_scope=[VT,DE], load_mode=full, invocation_mode=
tests. Overrides via env: PIPELINE_TEST_STATE_SCOPE (comma-separated),
PIPELINE_TEST_LOAD_MODE.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

import pytest

_KV_BY_ENV = {
    "dev": "kv-chpipeline-dev",
    "qa": "kv-chpipeline-qa",
    "prod": "kv-chpipeline-prod",
}
_WEBHOOK_SECRET_NAME = "PROVIDER-PIPELINE-WEBHOOK-URL"


def _az_secret_show(vault: str, name: str) -> str:
    """Fetch a KV secret value via `az keyvault secret show`. Returns the
    secret string. Raises pytest.fail on any az error so test output is
    actionable."""
    r = subprocess.run(
        ["az", "keyvault", "secret", "show",
         "--vault-name", vault, "--name", name,
         "--query", "value", "-o", "tsv"],
        capture_output=True, text=True,
        shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        pytest.fail(
            f"az keyvault secret show --vault-name {vault} --name {name} "
            f"exit={r.returncode}\nstderr={r.stderr[:400]}"
        )
    val = (r.stdout or "").strip()
    if not val:
        pytest.fail(f"empty value for KV secret {vault}/{name}")
    return val


def test_fire_provider_pipeline() -> None:
    env = os.environ.get("SMOKE_TEST_ENV", "dev").strip().lower()
    vault = _KV_BY_ENV.get(env)
    if not vault:
        pytest.skip(f"no KV vault name for env={env!r}")

    url = _az_secret_show(vault, _WEBHOOK_SECRET_NAME)

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
