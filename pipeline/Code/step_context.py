# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""StepContext and run manifest models for LLD v22 orchestration."""

from __future__ import annotations
from chathealthy_frontend_lib.exceptions import ChatHealthyException

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PipelineArgs:
    states: list[str]
    env_prefix: str = "dev"
    expected_duration_minutes: int = 120
    provider_collection: str | None = None
    metadata_collection: str = "admin.DataLoadMetadata"
    blob_container: str = "provider-data"
    state_scope: list[str] | None = None
    incremental: bool = False
    google_maps_enabled: bool = False
    embedding_enabled: bool = False
    batch_size: int = 1000
    pool_size: int = 51
    discrepancy_threshold: int = 1000
    resume_from_step: str | None = None
    run_id: str | None = None
    # Mandatory data-version integer. Default 0 is only for dataclass
    # field-ordering; __post_init__ enforces >= 1 at construction.
    data_version: int = 0
    # v42 test-injection hook: when True, normalize applies a hardcoded
    # inline mutation table (see normalize_provider_rows). Default False.
    # Temporary - the mutation code + this flag are expected to be
    # removed once the test each variance line supports has passed.
    testing_variance: bool = False

    def __post_init__(self):
        if not isinstance(self.data_version, int) or self.data_version < 1:
            raise ChatHealthyException(
                mode="value_error",
                message=(
                    "PipelineArgs.data_version is mandatory and must be an "
                    "int >= 1. Pass --data-version <n> on the CLI or set "
                    "DATA_VERSION env var."
                ),
                data_version=repr(self.data_version),
            )

    def resolved_states(self) -> list[str]:
        scope = self.state_scope or self.states
        if not scope:
            raise ChatHealthyException(mode="value_error", message="states or state_scope required")
        if len(scope) == 1 and scope[0].upper() == "ALL":
            from steps._partitions import ALL_US_STATES
            return list(ALL_US_STATES)
        return [s.upper() for s in scope]

    def provider_write_target(self) -> str:
        # Target production provider collection on the pipeline cluster.
        # Cluster: ChatHealthyPipelines. DB: PublicData. Collection:
        # Provider_v_{n}. A separate migration job moves this to the
        # front-end cluster for user-facing queries.
        if self.provider_collection:
            return self.provider_collection
        return f"PublicData.Provider_v_{self.data_version}"


@dataclass
class StepTransition:
    step: str
    status: str
    started_at: str
    finished_at: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class RunManifest:
    run_id: str
    pipeline_name: str
    env_prefix: str
    status: str = "running"
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    step_transitions: list[StepTransition] = field(default_factory=list)
    source_versions: dict[str, str] = field(default_factory=dict)
    discrepancy_summary: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    completed_steps: set[str] = field(default_factory=set)
    aca_job_resource_id: str | None = None

    @classmethod
    def new(cls, pipeline_name: str, args: PipelineArgs) -> "RunManifest":
        return cls(
            run_id=args.run_id or str(uuid.uuid4()),
            pipeline_name=pipeline_name,
            env_prefix=args.env_prefix,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pipeline_name": self.pipeline_name,
            "env_prefix": self.env_prefix,
            "env": self.env_prefix,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "step_transitions": [t.__dict__ for t in self.step_transitions],
            "source_versions": self.source_versions,
            "discrepancy_summary": self.discrepancy_summary,
            "metrics": self.metrics,
            "completed_steps": sorted(self.completed_steps),
            "aca_job_resource_id": self.aca_job_resource_id,
        }


@dataclass
class StepContext:
    args: PipelineArgs
    manifest: RunManifest
    config: dict[str, Any]
    mongo_client: Any
    blob_client: Any
    notification_client: Any | None = None
    catalog_cache: dict[str, dict[str, bool]] | None = None
    catalog: dict | None = None
    step_summaries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def run_id(self) -> str:
        return self.manifest.run_id

    @property
    def env_prefix(self) -> str:
        return self.args.env_prefix

    @property
    def provider_collection(self) -> str:
        return self.args.provider_write_target()

    @property
    def aca_job_resource_id(self) -> str | None:
        return self.manifest.aca_job_resource_id if self.manifest else None
