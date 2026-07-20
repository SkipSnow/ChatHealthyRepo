# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Regression: verify that a completed pipeline RUN_ID actually spawned
worker containers on the correct ACA env, and that env is bound to the
manifest-declared VNet + subnet.

Two independent assertions:

(1) container creation
    For a completed RUN_ID (env var VERIFY_RUN_ID), every worker step
    that was invoked with parallelism=process_pool MUST have at least
    one Azure Container Apps job execution whose env contains
    RUN_ID=<VERIFY_RUN_ID>. If _local_mode() suppressed the fan-out,
    zero worker executions land — the test fails loudly.

(2) VNet mapping
    Every ACA container app job in the pipeline resource group MUST run
    inside the ACA env declared by the manifest, and that env MUST be
    bound to the manifest's vnet_name + subnet_name. Assertion is
    against `az containerapp env show` — the live Azure state.

Skipped unless VERIFY_WORKER_CONTAINERS=1. Requires VERIFY_RUN_ID for
assertion (1); (2) runs independently of any RUN_ID.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MANIFEST = _REPO_ROOT / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
_ACA_ENV_TARGET_ID = "target_azure_container_apps_environment_pipeline"
_RG = "rg-chathealthy-pipeline-dev"


def _skip_unless_enabled():
    if os.environ.get("VERIFY_WORKER_CONTAINERS", "").strip() != "1":
        pytest.skip("VERIFY_WORKER_CONTAINERS not set to 1")


def _load_manifest() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def _find_aca_env_binding(env_prefix: str = "dev") -> dict:
    for tgt in _load_manifest()["DeploymentTargetRecord"]:
        if tgt.get("target_id") != _ACA_ENV_TARGET_ID:
            continue
        for eb in tgt.get("environments", []):
            if eb.get("env_binding") == env_prefix:
                return eb
    raise AssertionError(
        f"manifest missing {_ACA_ENV_TARGET_ID} binding for env={env_prefix}"
    )


def _az_json(argv: list[str]) -> object:
    proc = subprocess.run(
        ["az", *argv, "-o", "json"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"az call failed: {' '.join(argv)}\nstderr: {proc.stderr.strip()[:500]}"
        )
    return json.loads(proc.stdout or "null")


def _worker_job_names(env_binding: dict) -> list[str]:
    names: list[str] = []
    for pkg in env_binding.get("packages", []) or []:
        cfg = pkg.get("config") or {}
        if cfg.get("role") == "worker":
            job_name = cfg.get("job_name")
            if job_name:
                names.append(job_name)
    return names


def test_worker_containers_spawned_for_run():
    _skip_unless_enabled()
    run_id = os.environ.get("VERIFY_RUN_ID", "").strip()
    if not run_id:
        pytest.skip("VERIFY_RUN_ID not set — cannot assert per-run worker spawn")

    eb = _find_aca_env_binding("dev")
    workers = _worker_job_names(eb)
    assert workers, "manifest declares no worker jobs under the ACA env packages"

    per_job_run_counts: dict[str, int] = {}
    for job_name in workers:
        execs = _az_json([
            "containerapp", "job", "execution", "list",
            "-g", _RG, "-n", job_name,
        ]) or []
        matched = 0
        for e in execs:
            env_list = (
                (((e.get("properties") or {}).get("template")) or {})
                .get("containers", [{}])[0]
                .get("env", []) or []
            )
            if any(v.get("name") == "RUN_ID" and v.get("value") == run_id for v in env_list):
                matched += 1
        per_job_run_counts[job_name] = matched

    fanned_out = {j: n for j, n in per_job_run_counts.items() if n > 0}
    assert fanned_out, (
        f"No worker ACA job saw an execution tagged RUN_ID={run_id}. "
        f"Either _local_mode() suppressed fan-out (set ACA_JOB_DEPLOY=1 on "
        f"Control's env), Control abended before reaching a process_pool "
        f"step, or the RUN_ID env var isn't being merged into worker "
        f"executions. Per-job execution counts for this RUN_ID: "
        f"{per_job_run_counts!r}"
    )


def test_aca_env_bound_to_manifest_vnet_and_subnet():
    _skip_unless_enabled()
    eb = _find_aca_env_binding("dev")
    env_block = eb.get("azure_container_apps_environment") or {}
    manifest_env_name = env_block.get("environment_name")
    manifest_vnet = env_block.get("vnet_name")
    manifest_subnet = env_block.get("subnet_name")
    assert manifest_env_name and manifest_vnet and manifest_subnet, (
        "manifest ACA env block missing environment_name / vnet_name / "
        "subnet_name"
    )

    live = _az_json([
        "containerapp", "env", "show",
        "-g", _RG, "-n", manifest_env_name,
    ]) or {}
    infra = ((live.get("properties") or {}).get("vnetConfiguration") or {})
    subnet_id = infra.get("infrastructureSubnetId", "")
    assert subnet_id, (
        f"live ACA env {manifest_env_name!r} has no infrastructureSubnetId "
        "(env not attached to a VNet — deploy chain must bind it)"
    )
    assert f"/virtualNetworks/{manifest_vnet}/" in subnet_id, (
        f"live ACA env subnet id {subnet_id!r} does not reference the "
        f"manifest-declared vnet_name {manifest_vnet!r}"
    )
    assert subnet_id.endswith(f"/subnets/{manifest_subnet}"), (
        f"live ACA env subnet id {subnet_id!r} does not reference the "
        f"manifest-declared subnet_name {manifest_subnet!r}"
    )


def test_every_pipeline_aca_job_lives_in_declared_env():
    _skip_unless_enabled()
    eb = _find_aca_env_binding("dev")
    env_block = eb.get("azure_container_apps_environment") or {}
    manifest_env_name = env_block.get("environment_name")
    assert manifest_env_name, "manifest ACA env block missing environment_name"

    all_jobs = _az_json([
        "containerapp", "job", "list", "-g", _RG,
    ]) or []
    off_env: list[str] = []
    for j in all_jobs:
        name = j.get("name", "")
        env_id = ((j.get("properties") or {}).get("environmentId") or "")
        if f"/managedEnvironments/{manifest_env_name}" not in env_id:
            off_env.append(f"{name} -> {env_id!r}")
    assert not off_env, (
        f"ACA jobs in {_RG} live outside the manifest-declared ACA env "
        f"{manifest_env_name!r}: {off_env}"
    )
