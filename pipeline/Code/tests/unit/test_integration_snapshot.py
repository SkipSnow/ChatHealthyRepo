# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from pipeline_integration_snapshot import write_integration_snapshot
from pathlib import Path
def test_write(tmp_path):
    p = write_integration_snapshot(tmp_path / 's.json', {'a': 1})
    assert Path(p).exists()
