"""Typed data classes for one Deployment record.

Mirrors the canonical schema at
`Website/schemas/ChatHealthyDeploymentArchitectureSchema.json`
($id `https://dev.chathealthy.ai/schemas/ChatHealthyDeploymentArchitectureSchema.json`).

One `TargetRecord` is one deployment target's full definition across every
environment in which that target is realized, plus the files that compose
the target. A `DeploymentCollection` is the set of all `TargetRecord`s for
a deployment.

There is no `metadata` field anywhere. Processing/transformation logic
lives in software, not in this data shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator


@dataclass(slots=True)
class EnvironmentBinding:
    """One environment binding for a TargetRecord."""

    env_binding: str  # closed enum: local|dev|qa|prod
    node_address: str  # addressable location; {env} substitution allowed
    azure: dict | None = None  # present iff target_kind=='azure_function_app';
                                # keys: resource_group, function_app, task_hub
    azure_container_app: dict | None = None
                                # present iff target_kind=='azure_container_app';
                                # keys: resource_group, container_app,
                                # container_app_environment, task_hub,
                                # min_replicas, max_replicas, cpu, memory_gi
    branch: str | None = None   # source git branch this env deploys from;
                                 # required for git-branch-bound target_kinds
                                 # (cloudflare_pages_project, github_actions_workflow_runner)
                                 # per REQ-T-050

    def to_dict(self) -> dict:
        out: dict = {"env_binding": self.env_binding, "node_address": self.node_address}
        if self.azure is not None:
            out["azure"] = self.azure
        if self.azure_container_app is not None:
            out["azure_container_app"] = self.azure_container_app
        if self.branch is not None:
            out["branch"] = self.branch
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "EnvironmentBinding":
        return cls(
            env_binding=d["env_binding"],
            node_address=d["node_address"],
            azure=d.get("azure"),
            azure_container_app=d.get("azure_container_app"),
            branch=d.get("branch"),
        )


@dataclass(slots=True)
class FileComposition:
    """One file that composes a TargetRecord.

    disposition decides who owns the bytes:
      - 'managed': JSON owns the bytes via embedded_content. Crosswalk
        requires byte-equality with the repo tree. Extractor materializes.
      - 'referenced': only source_location is declared. embedded_content
        is None. Deploy copies bytes from origin/<branch>'s tree at
        deploy time. Crosswalk validates presence only.
    """

    feature_id: str  # regex-shape ref into agile_backlog
    source_location: str  # repo-relative path; forward slashes
    handler_type: str  # closed enum: python_source|...|cert_or_key
    disposition: str  # closed enum: managed|referenced
    embedded_content: str | None = None  # present iff disposition='managed'
    # layout: structured spec from which the Builder generates the
    # file's embedded_content. Required when disposition='managed' AND
    # handler_type in {'dockerfile','workflow_yaml'}. Polymorphic:
    # array for dockerfile (instruction list), object for workflow_yaml.
    layout: object | None = None
    # content_hash: sha256 hex digest of the file's bytes at deploy
    # time. Builder populates for disposition='referenced' (managed
    # files are protected by embedded_content). Crosswalk validates
    # the disk content still hashes to this value, catching cases
    # where on-disk content drifts from the committed/JSON state.
    content_hash: str | None = None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "feature_id": self.feature_id,
            "source_location": self.source_location,
            "handler_type": self.handler_type,
            "disposition": self.disposition,
        }
        if self.disposition == "managed":
            if self.embedded_content is None:
                raise ValueError(
                    f"FileComposition {self.source_location!r}: disposition="
                    f"'managed' requires embedded_content"
                )
            out["embedded_content"] = self.embedded_content
            if self.handler_type in ("dockerfile", "workflow_yaml"):
                if self.layout is None:
                    raise ValueError(
                        f"FileComposition {self.source_location!r}: "
                        f"managed {self.handler_type} requires layout"
                    )
                out["layout"] = self.layout
            elif self.layout is not None:
                out["layout"] = self.layout
        elif self.disposition == "referenced":
            if self.embedded_content is not None:
                raise ValueError(
                    f"FileComposition {self.source_location!r}: disposition="
                    f"'referenced' forbids embedded_content"
                )
            if self.layout is not None:
                raise ValueError(
                    f"FileComposition {self.source_location!r}: disposition="
                    f"'referenced' forbids layout"
                )
            if self.content_hash is not None:
                out["content_hash"] = self.content_hash
        else:
            raise ValueError(
                f"FileComposition {self.source_location!r}: unknown "
                f"disposition {self.disposition!r}"
            )
        return out

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> "FileComposition":
        return cls(
            feature_id=str(d["feature_id"]),
            source_location=str(d["source_location"]),
            handler_type=str(d["handler_type"]),
            disposition=str(d["disposition"]),
            embedded_content=d.get("embedded_content"),  # type: ignore[arg-type]
            layout=d.get("layout"),
            content_hash=d.get("content_hash"),  # type: ignore[arg-type]
        )


@dataclass(slots=True)
class TargetRecord:
    """One Deployment target's full record."""

    target_id: str  # opaque; pattern ^target_[a-z0-9_]{1,64}$
    target_kind: str  # closed enum: hf_space|cloudflare_pages_project|...
    environments: list[EnvironmentBinding]
    files: list[FileComposition]
    secrets: dict | None = None  # name -> store_id; same keys apply across all envs;
                                  # per-env values resolved via SecretsResolver at deploy time

    def env_binding_set(self) -> set[str]:
        return {e.env_binding for e in self.environments}

    def file_by_source_location(self, source_location: str) -> FileComposition | None:
        for f in self.files:
            if f.source_location == source_location:
                return f
        return None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "target_id": self.target_id,
            "target_kind": self.target_kind,
            "environments": [e.to_dict() for e in self.environments],
            "files": [f.to_dict() for f in self.files],
        }
        if self.secrets:
            out["secrets"] = self.secrets
        return out

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> "TargetRecord":
        envs_raw = d["environments"]
        files_raw = d["files"]
        if not isinstance(envs_raw, list) or not isinstance(files_raw, list):
            raise TypeError("environments and files must be lists")
        return cls(
            target_id=str(d["target_id"]),
            target_kind=str(d["target_kind"]),
            environments=[EnvironmentBinding.from_dict(e) for e in envs_raw],
            files=[FileComposition.from_dict(f) for f in files_raw],
            secrets=d.get("secrets"),  # type: ignore[arg-type]
        )


@dataclass(slots=True)
class DeploymentCollection:
    """The set of TargetRecords that constitutes one deployment.

    Persisted to `brain/machine_artifacts/content/deployment_architecture.json`
    as a JSON array of TargetRecord documents.
    """

    records: list[TargetRecord] = field(default_factory=list)

    def __iter__(self) -> Iterator[TargetRecord]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def add(self, record: TargetRecord) -> None:
        for existing in self.records:
            if existing.target_id == record.target_id:
                raise ValueError(
                    f"target_id collision: {record.target_id!r} already in collection"
                )
        self.records.append(record)

    def by_target_id(self, target_id: str) -> TargetRecord | None:
        for r in self.records:
            if r.target_id == target_id:
                return r
        return None

    def all_feature_ids(self) -> set[str]:
        return {f.feature_id for r in self.records for f in r.files}

    def all_source_locations(self) -> set[str]:
        return {f.source_location for r in self.records for f in r.files}

    def to_list(self) -> list[dict[str, object]]:
        return [r.to_dict() for r in self.records]

    @classmethod
    def from_list(cls, data: list[dict[str, object]]) -> "DeploymentCollection":
        coll = cls()
        for d in data:
            coll.add(TargetRecord.from_dict(d))
        return coll
