# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from provider_pipeline_orchestrator import ProviderPipelineOrchestrator
def test_step_count():
    assert len(ProviderPipelineOrchestrator.STEPS) == 20
