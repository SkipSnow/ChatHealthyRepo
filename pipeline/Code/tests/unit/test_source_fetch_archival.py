# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from pipeline_source_archival import archive_source_blob_path
def test_path():
    p = archive_source_blob_path(
        env_prefix="dev",
        source_name="nppes_npi",
        version="2026-07",
        filename="npi.zip",
    )
    assert p == 'dev-pipeline-sources/nppes_npi/2026-07/npi.zip'
