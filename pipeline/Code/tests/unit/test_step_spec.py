# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from step_spec import StepSpec
def test_step_spec_fields():
    s = StepSpec('prepare_infrastructure', aca_job_name='prov-control')
    assert s.name == 'prepare_infrastructure'
    assert s.parallelism == 'serial'
