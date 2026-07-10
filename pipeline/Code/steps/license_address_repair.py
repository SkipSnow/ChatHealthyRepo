# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from license_address_repair import repair_license_addresses

def run_step(ctx):
    return repair_license_addresses(ctx)


def execute(ctx):
    return run_step(ctx)
