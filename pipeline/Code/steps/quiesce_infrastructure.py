# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from aca_job_manager import AcaJobManager
from atlas_cluster_manager import scale_to_post_job
from pipeline_runtime import PipelineRuntime


def execute(ctx):
    rt = PipelineRuntime(ctx)
    rt.reservations_collection().delete_many({"job_id": ctx.run_id})
    if ctx.manifest.aca_job_resource_id:
        AcaJobManager(ctx.config).delete_job(ctx.manifest.aca_job_resource_id)
    # Post-job scale-down per operator directive 2026-08-01:
    # if the pipeline cluster is at M80 (JOB_TIER), resize to M30. Guard
    # inside scale_to_post_job skips if the cluster is on any other tier.
    try:
        cluster = ctx.config.get("pipeline_cluster") or "ChatHealthyDataPipelines"
        scale_to_post_job(cluster)
    except Exception:  # noqa: BLE001
        # quiesce MUST NOT fail the run because of a post-job resize
        # error; the reaper carries the same M80->M30 guard as a safety
        # net on the next 15-min tick.
        pass
    return {"released": True}
