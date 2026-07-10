# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

import pytest

from pipeline_config import _DEFAULT_PROVIDER_CONFIG


@pytest.mark.unit
def test_default_config():
    assert _DEFAULT_PROVIDER_CONFIG['pipeline_name'] == 'provider'
