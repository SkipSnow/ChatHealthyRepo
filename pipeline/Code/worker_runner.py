# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

import logging
import os
import sys

from pipeline_config import load_pipeline_config
from pipeline_env import require_env
from pipeline_runtime import PipelineRuntime
from provider_pipeline_orchestrator import ProviderPipelineOrchestrator
from step_context import PipelineArgs, RunManifest
from work_item_claim import claim_work_item, complete_work_item, fail_work_item
from steps import STEP_RUNNERS

_log = logging.getLogger("worker_runner")


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    env = require_env("ENV_PREFIX")
    run_id = require_env("RUN_ID")
    stage = require_env("PIPELINE_STAGE")
    config = load_pipeline_config(env, "provider")
    runtime = PipelineRuntime(env, config)
    doc = runtime.runs_collection().find_one({"run_id": run_id})
    if not doc:
        raise SystemExit(f"run manifest not found: {run_id}")
    args = PipelineArgs(env=env, load_mode=doc.get("load_mode", "full"), state_scope=doc.get("state_scope", ["ALL"]))
    manifest = ProviderPipelineOrchestrator(env, config, None)._manifest_from_doc(doc)  # type: ignore[arg-type]
    item = claim_work_item(runtime.work_items_collection(), run_id=run_id, stage=stage)
    if not item:
        _log.info("no pending work item")
        return 0
    step_name = os.environ.get("STEP_NAME", stage)
    ctx = ProviderPipelineOrchestrator(env, config, None).build_step_context(args, manifest, step_name)
    ctx.work_item = item
    ctx.partition_id = item.get("partition_id")
    try:
        out = STEP_RUNNERS[step_name](ctx) or {}
        complete_work_item(runtime.work_items_collection(), item["_id"], out)
        return 0
    except Exception as exc:
        fail_work_item(runtime.work_items_collection(), item["_id"], code=type(exc).__name__, message=str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
