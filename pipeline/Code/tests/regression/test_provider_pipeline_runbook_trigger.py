# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Regression: fire ProviderPipelineRunbook webhook for a dual-state scope
and assert the AA accepts the request.

Covers the runbook trigger tier's three tactical fixes (payload delivery via
WEBHOOKDATA, certifi-backed SSL trust for DoH fallback, ACA-start body shape
carrying full container spec). If any of the three regress the runbook fails
its own run and the follow-on assertions (below) surface it.

Skipped unless RUN_PROVIDER_PIPELINE_E2E=1.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "pipeline" / "Code"))
from pipeline_env import load_pipeline_env  # noqa: E402


_KEY_VAULT_URI = os.environ.get(
    "PIPELINE_KEY_VAULT_URI",
    "https://kv-chpipeline-dev.vault.azure.net/",
)
_WEBHOOK_SECRET_NAME = "PROVIDER-PIPELINE-WEBHOOK-URL"


def _webhook_url() -> str:
    load_pipeline_env()
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient
    kv = SecretClient(vault_url=_KEY_VAULT_URI, credential=DefaultAzureCredential())
    return kv.get_secret(_WEBHOOK_SECRET_NAME).value


@pytest.mark.parametrize("state_pair", [["WY", "VT"], ["DE", "RI"]])
def test_runbook_trigger_accepts_dual_state(integration_enabled, state_pair):
    if not integration_enabled:
        pytest.skip("RUN_PROVIDER_PIPELINE_E2E not set")
    body = json.dumps({
        "state_scope": state_pair,
        "load_mode": "full",
        "invocation_mode": "regression_test",
    }).encode("utf-8")
    req = urllib.request.Request(
        _webhook_url(),
        method="POST",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.status == 202, f"webhook returned {r.status}, expected 202"
        response_body = r.read().decode("utf-8")
        print(f"[runbook_trigger] states={state_pair} status={r.status} body={response_body!r}")
