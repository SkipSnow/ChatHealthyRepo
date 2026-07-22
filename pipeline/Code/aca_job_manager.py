# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ACA Container Apps Job lifecycle — LLD v22 §2.2 / §2.3.

Local / PIPELINE_LOCAL_MODE runs use in-process thread pools (no ARM calls).
Deployed runs provision ephemeral ACA jobs per stage when ACA_JOB_DEPLOY=1.
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException

import json

import os
import subprocess
from typing import Any

_log = ChatHealthyLoggingService()

ACA_RESOURCE_GROUP = os.environ.get("ACA_RESOURCE_GROUP", "ChatHealthyPipelineRG")
ACA_ENVIRONMENT = os.environ.get("ACA_ENVIRONMENT", "chathealthy-pipeline-env")
ACA_IMAGE = os.environ.get(
    "ACA_PIPELINE_IMAGE",
    "chathealthypipelineacr.azurecr.io/prov-pipeline:latest",
)


def _local_mode() -> bool:
    """True iff we should run worker steps in-process instead of fanning out
    to real ACA job executions. Order of precedence:
      1. Explicit opt-out ACA_JOB_DEPLOY=1 => real ACA fan-out even in dev.
      2. Explicit opt-in PIPELINE_LOCAL_MODE / PIPELINE_TEST_MODE => local.
      3. Running INSIDE an ACA execution (CONTAINER_APP_JOB_EXECUTION_NAME is
         set by the ACA runtime) => real ACA fan-out. Control-in-ACA must
         spawn workers-in-ACA.
      4. Default => local (safe on a workstation with no ACA runtime).
    """
    if os.environ.get("ACA_JOB_DEPLOY", "").lower() in ("1", "true", "yes"):
        return False
    if os.environ.get("PIPELINE_LOCAL_MODE", "").lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("PIPELINE_TEST_MODE", "").lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("CONTAINER_APP_JOB_EXECUTION_NAME", "").strip():
        return False
    return True


def _az(args: list[str]) -> dict[str, Any]:
    cmd = ["az", *args, "-o", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise ChatHealthyException(mode="runtime_error", message=f"az {' '.join(args)} failed ({proc.returncode}): {proc.stderr[:500]}"
        )
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def provision_job(
    job_name: str,
    *,
    parallelism: int = 1,
    cpu: float = 2.0,
    memory: str = "4Gi",
) -> dict[str, Any]:
    """Create or update an ACA Job definition for a pipeline stage."""
    if _local_mode():
        _log.info("ACA provision no-op (local mode): %s parallelism=%s", job_name, parallelism)
        return {"job_name": job_name, "mode": "local", "parallelism": parallelism}

    try:
        _az([
            "containerapp", "job", "create",
            "--name", job_name,
            "--resource-group", ACA_RESOURCE_GROUP,
            "--environment", ACA_ENVIRONMENT,
            "--trigger-type", "Manual",
            "--replica-timeout", "7200",
            "--replica-retry-limit", "0",
            "--parallelism", str(parallelism),
            "--replica-completion-count", str(parallelism),
            "--image", ACA_IMAGE,
            "--cpu", str(cpu),
            "--memory", memory,
            "--command", "python", "worker_runner.py",
        ])
    except RuntimeError:
        _az([
            "containerapp", "job", "update",
            "--name", job_name,
            "--resource-group", ACA_RESOURCE_GROUP,
            "--parallelism", str(parallelism),
            "--replica-completion-count", str(parallelism),
            "--image", ACA_IMAGE,
        ])
    return {"job_name": job_name, "mode": "aca", "parallelism": parallelism}


def start_job(
    job_name: str,
    *,
    run_id: str,
    step: str,
    env_prefix: str,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Start an ACA Job execution for one pipeline stage."""
    if _local_mode():
        return {"job_name": job_name, "mode": "local", "run_id": run_id, "step": step}

    env_vars = [
        f"PIPELINE_WORKER_MODE=worker",
        f"RUN_ID={run_id}",
        f"ENV_PREFIX={env_prefix}",
        f"PIPELINE_STEP={step}",
    ]
    for pass_through in ("PIPELINE_NAME", "CHATHEALTHY_VNET_NAME"):
        v = os.environ.get(pass_through, "").strip()
        if v:
            env_vars.append(f"{pass_through}={v}")
    for k, v in (extra_env or {}).items():
        env_vars.append(f"{k}={v}")
    result = _az([
        "containerapp", "job", "start",
        "--name", job_name,
        "--resource-group", ACA_RESOURCE_GROUP,
        "--args", "--run-id", run_id, "--step", step, "--env-prefix", env_prefix,
    ])
    return {"job_name": job_name, "execution": result}


def delete_job(job_name: str) -> dict[str, Any]:
    """Delete an ephemeral ACA Job after the stage completes."""
    if _local_mode():
        _log.info("ACA delete no-op (local mode): %s", job_name)
        return {"job_name": job_name, "deleted": False, "mode": "local"}
    _az([
        "containerapp", "job", "delete",
        "--name", job_name,
        "--resource-group", ACA_RESOURCE_GROUP,
        "--yes",
    ])
    return {"job_name": job_name, "deleted": True, "mode": "aca"}


def resume_job(job_name: str, execution_name: str) -> dict[str, Any]:
    """Resume a suspended ACA Job execution (LLD §5.4 resumability)."""
    if _local_mode():
        return {"job_name": job_name, "execution_name": execution_name, "mode": "local"}
    result = _az([
        "containerapp", "job", "execution", "resume",
        "--name", job_name,
        "--resource-group", ACA_RESOURCE_GROUP,
        "--job-execution-name", execution_name,
    ])
    return {"job_name": job_name, "execution": result}


class AcaJobManager:
    def __init__(self, config=None):
        self.config = config or {}

    def delete_job(self, job_name: str):
        return delete_job(job_name)
