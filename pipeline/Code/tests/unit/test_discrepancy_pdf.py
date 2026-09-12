# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from chathealthy_lib.discrepancy_pdf import build_discrepancy_pdf
def test_pdf_bytes():
    b = build_discrepancy_pdf({'run_id': 'r1', 'status': 'success', 'env': 'dev'}, [])
    assert isinstance(b, bytes) and len(b) > 0
