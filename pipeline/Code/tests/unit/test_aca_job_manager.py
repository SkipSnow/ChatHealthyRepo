# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""LLD §9.2 — ACA job manager local mode."""

import os

import pytest


@pytest.mark.unit
def test_aca_provision_local_mode(monkeypatch):
    monkeypatch.setenv("PIPELINE_TEST_MODE", "1")
    monkeypatch.delenv("ACA_JOB_DEPLOY", raising=False)
    from aca_job_manager import provision_job, delete_job, start_job

    out = provision_job("prov-test", parallelism=4)
    assert out["mode"] == "local"
    assert start_job("prov-test", run_id="r1", step="county_enrichment", env_prefix="dev")["mode"] == "local"
    assert delete_job("prov-test")["mode"] == "local"
