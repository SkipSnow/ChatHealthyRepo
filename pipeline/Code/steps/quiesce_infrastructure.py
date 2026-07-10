# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from aca_job_manager import AcaJobManager
from pipeline_runtime import PipelineRuntime


def execute(ctx):
    rt = PipelineRuntime(ctx)
    rt.reservations_collection().delete_many({"job_id": ctx.run_id})
    if ctx.manifest.aca_job_resource_id:
        AcaJobManager(ctx.config).delete_job(ctx.manifest.aca_job_resource_id)
    return {"released": True}
