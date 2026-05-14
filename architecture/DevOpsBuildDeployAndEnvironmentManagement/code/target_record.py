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

    def to_dict(self) -> dict[str, str]:
        return {"env_binding": self.env_binding, "node_address": self.node_address}

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "EnvironmentBinding":
        return cls(env_binding=d["env_binding"], node_address=d["node_address"])


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

    def to_dict(self) -> dict[str, str]:
        out: dict[str, str] = {
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
        elif self.disposition == "referenced":
            if self.embedded_content is not None:
                raise ValueError(
                    f"FileComposition {self.source_location!r}: disposition="
                    f"'referenced' forbids embedded_content"
                )
        else:
            raise ValueError(
                f"FileComposition {self.source_location!r}: unknown "
                f"disposition {self.disposition!r}"
            )
        return out

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "FileComposition":
        return cls(
            feature_id=d["feature_id"],
            source_location=d["source_location"],
            handler_type=d["handler_type"],
            disposition=d["disposition"],
            embedded_content=d.get("embedded_content"),
        )


@dataclass(slots=True)
class TargetRecord:
    """One Deployment target's full record."""

    target_id: str  # opaque; pattern ^target_[a-z0-9_]{1,64}$
    target_kind: str  # closed enum: hf_space|cloudflare_pages_project|...
    environments: list[EnvironmentBinding]
    files: list[FileComposition]

    def env_binding_set(self) -> set[str]:
        return {e.env_binding for e in self.environments}

    def file_by_source_location(self, source_location: str) -> FileComposition | None:
        for f in self.files:
            if f.source_location == source_location:
                return f
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "target_kind": self.target_kind,
            "environments": [e.to_dict() for e in self.environments],
            "files": [f.to_dict() for f in self.files],
        }

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
