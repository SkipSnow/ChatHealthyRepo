# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Parallelism = Literal["serial", "gather", "process_pool"]
InvocationPhase = Literal["main_loop", "finally_block"]


@dataclass(frozen=True)
class StepSpec:
    name: str
    prerequisites: list[str] = field(default_factory=list)
    parallelism: Parallelism = "serial"
    invocation_phase: InvocationPhase = "main_loop"
    aca_job_name: str | None = None
    partition_key: str | None = None
    max_attempts: int = 3
    backoff_seconds: float = 5.0
    stall_threshold_seconds: int = 0
    expected_duration_seconds: int = 300
    batch_call_target: str | None = None
