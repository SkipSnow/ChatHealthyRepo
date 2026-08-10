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

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "FrontEndApplicationLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()

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
    # Mandatory. Operator must set PIPELINE_TEST_DATA_VERSION so the
    # pipeline knows which versioned staging + target collections to
    # write. There is no default -- writing to the wrong version is
    # exactly the class of bug we are trying to prevent.
    dv_raw = os.environ.get("PIPELINE_TEST_DATA_VERSION", "").strip()
    if not dv_raw.isdigit() or int(dv_raw) < 1:
        pytest.fail(
            "PIPELINE_TEST_DATA_VERSION env var is required (int >= 1). "
            "Example: PIPELINE_TEST_DATA_VERSION=3"
        )
    data_version = int(dv_raw)
    # google_maps_enabled: keep OFF for the test fire by default (paid stage).
    # Operator can flip on via PIPELINE_TEST_GOOGLE_MAPS_ENABLED={1,true,yes}.
    gm_raw = os.environ.get("PIPELINE_TEST_GOOGLE_MAPS_ENABLED", "").strip().lower()
    google_maps_enabled = gm_raw in ("1", "true", "yes")
    payload = {
        "state_scope": state_scope,
        "load_mode": load_mode,
        "invocation_mode": "tests",
        "data_version": data_version,
        "google_maps_enabled": google_maps_enabled,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, method="POST", data=body,
        headers={"Content-Type": "application/json"},
    )
    _CH_LOG.info(f"[fire_provider_pipeline] POST env={env} payload={payload}")
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.status == 202, f"expected HTTP 202, got {r.status}"
        resp = r.read().decode("utf-8")
        _CH_LOG.info(f"[fire_provider_pipeline] HTTP 202 response={resp}")
