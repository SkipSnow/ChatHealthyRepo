# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Regression: fire ProviderPipelineRunbook webhook once with dual state
scope [VT, DE] and assert the AA accepts the request.

Covers the runbook trigger tier's three tactical fixes (payload delivery via
WEBHOOKDATA, certifi-backed SSL trust for DoH fallback, ACA-start body shape
carrying full container spec). Trigger-side assertion only: HTTP 202 accept.
Runbook-side execution assertions (state_scope preserved, no SSL error,
Control ACA started) live downstream in the pipeline run manifest.

Skipped unless RUN_PROVIDER_PIPELINE_E2E=1.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "ChatHealthyLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "pipeline" / "Code"))
from pipeline_env import load_pipeline_env  # noqa: E402


_KEY_VAULT_URI = os.environ.get(
    "PIPELINE_KEY_VAULT_URI",
    "https://kv-chpipeline-dev.vault.azure.net/",
)
_WEBHOOK_SECRET_NAME = "PROVIDER-PIPELINE-WEBHOOK-URL"
_STATE_SCOPE = ["VT", "DE"]


def _webhook_url() -> str:
    load_pipeline_env()
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient
    kv = SecretClient(vault_url=_KEY_VAULT_URI, credential=DefaultAzureCredential())
    return kv.get_secret(_WEBHOOK_SECRET_NAME).value


def test_runbook_trigger_accepts_dual_state(integration_enabled):
    if not integration_enabled:
        pytest.skip("RUN_PROVIDER_PIPELINE_E2E not set")
    body = json.dumps({
        "state_scope": _STATE_SCOPE,
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
        _CH_LOG.info(f"[runbook_trigger] states={_STATE_SCOPE} status={r.status} body={response_body!r}")
