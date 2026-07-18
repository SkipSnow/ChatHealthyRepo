# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Parameterized operator utility: stop an in-flight ACA job execution
via the ARM REST API.

Invocation:
    KILL_JOB_NAME=job-chp-control-dev \
    KILL_JOB_EXECUTION_NAME=job-chp-control-dev-awnzca4 \
    python -m pytest pipeline/Code/tests/regression/kill_aca_job_execution.py -v -s

Uses `DefaultAzureCredential` (workstation `az login`) so it operates
under the operator's identity, not a shared service principal. Non-
mutating discovery (execution show) runs first to fail fast if the
execution is already terminal; the mutating stop runs only if the
execution is still `Running` or `Processing`.
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


_SUBSCRIPTION_ID = os.environ.get(
    "PIPELINE_SUBSCRIPTION_ID",
    "7a17eec1-c477-4c7c-b1c1-d0662ce7a1ee",
)
_RESOURCE_GROUP = os.environ.get(
    "PIPELINE_RESOURCE_GROUP",
    "rg-chathealthy-pipeline-dev",
)
_API_VERSION = "2024-03-01"
_TERMINAL_STATUSES = {"Succeeded", "Failed", "Stopped", "Degraded"}


def _bearer_token() -> str:
    load_pipeline_env()
    from azure.identity import DefaultAzureCredential
    cred = DefaultAzureCredential()
    return cred.get_token("https://management.azure.com/.default").token


def _execution_show(job_name: str, execution_name: str) -> dict:
    tok = _bearer_token()
    url = (
        f"https://management.azure.com/subscriptions/{_SUBSCRIPTION_ID}"
        f"/resourceGroups/{_RESOURCE_GROUP}/providers/Microsoft.App/jobs/"
        f"{job_name}/executions/{execution_name}?api-version={_API_VERSION}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _execution_stop(job_name: str, execution_name: str) -> int:
    tok = _bearer_token()
    url = (
        f"https://management.azure.com/subscriptions/{_SUBSCRIPTION_ID}"
        f"/resourceGroups/{_RESOURCE_GROUP}/providers/Microsoft.App/jobs/"
        f"{job_name}/executions/{execution_name}/stop?api-version={_API_VERSION}"
    )
    req = urllib.request.Request(
        url,
        method="POST",
        headers={"Authorization": f"Bearer {tok}", "Content-Length": "0"},
        data=b"",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def test_kill_aca_job_execution():
    execution_name = os.environ.get("KILL_JOB_EXECUTION_NAME", "").strip()
    if not execution_name:
        pytest.skip("KILL_JOB_EXECUTION_NAME not set")
    job_name = os.environ.get("KILL_JOB_NAME", "job-chp-control-dev").strip()

    exec_doc = _execution_show(job_name, execution_name)
    status = (exec_doc.get("properties") or {}).get("status", "")
    print(f"[kill] job={job_name} execution={execution_name} current_status={status!r}")
    if status in _TERMINAL_STATUSES:
        print(f"[kill] execution already terminal ({status}); no-op")
        return

    http_status = _execution_stop(job_name, execution_name)
    assert http_status in (200, 202), f"stop returned {http_status}, expected 200 or 202"
    print(f"[kill] stop accepted (HTTP {http_status}); execution transitioning to Stopped")
