"""Typed loader for `agile_backlog.json` — the business model side of the
two-model deployment reconciliation.

The Crosswalk consumes both `AgileBacklog` and a `DeploymentCollection`;
no other deploy-chain code reads agile_backlog.json directly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(slots=True)
class Requirement:
    req_id: str
    name: str
    requirement: str
    priority: str
    status: str
    approval: str
    orphan: bool
    feature_id: str

    @classmethod
    def from_raw(cls, raw: dict[str, object], feature_id: str) -> "Requirement":
        return cls(
            req_id=str(raw.get("req_id", "")),
            name=str(raw.get("name", "")),
            requirement=str(raw.get("requirement", "")),
            priority=str(raw.get("priority", "")),
            status=str(raw.get("status", "")),
            approval=str(raw.get("approval", "")),
            orphan=bool(raw.get("orphan", False)),
            feature_id=feature_id,
        )


@dataclass(slots=True)
class AgileBacklog:
    """Read-only typed view over `agile_backlog.json`.

    The constructor accepts the parsed dict; use `AgileBacklogLoader.load`
    to load + validate from disk.
    """

    raw: dict[str, object]
    _reqs_by_id: dict[str, Requirement] = field(default_factory=dict)
    _feature_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        for epic in self.raw.get("epics", {}).get("epic", []):
            for feature in epic.get("features", {}).get("feature", []):
                fid = str(feature.get("feature_id", ""))
                if not fid:
                    continue
                self._feature_ids.add(fid)
                for story in feature.get("stories", {}).get("story", []):
                    for r in story.get("requirements", {}).get("requirement", []):
                        req = Requirement.from_raw(r, fid)
                        if req.req_id:
                            self._reqs_by_id[req.req_id] = req

    def req_by_id(self, req_id: str) -> Requirement | None:
        return self._reqs_by_id.get(req_id)

    def feature_id_set(self) -> set[str]:
        return set(self._feature_ids)

    def iter_active_reqs_in_feature(
        self, feature_id: str
    ) -> Iterable[Requirement]:
        for req in self._reqs_by_id.values():
            if req.feature_id != feature_id:
                continue
            if req.status in ("in_progress", "done") and req.approval == "approved":
                yield req


class AgileBacklogLoader:
    """Loads + schema-validates `agile_backlog.json`."""

    def __init__(self, schema_uri: str | Path) -> None:
        from jsonschema import Draft202012Validator
        with Path(schema_uri).open("r", encoding="utf-8") as f:
            self._schema = json.load(f)
        self._validator = Draft202012Validator(self._schema)
        self.schema_uri: str = str(schema_uri)

    def load(self, path: Path) -> AgileBacklog:
        with path.open("r", encoding="utf-8") as f:
            doc = json.load(f)
        errors = sorted(
            self._validator.iter_errors(doc), key=lambda e: list(e.path)
        )
        if errors:
            joined = "; ".join(
                f"{list(e.path)}: {e.message}" for e in errors[:5]
            )
            raise ValueError(f"agile_backlog failed schema validation: {joined}")
        if not isinstance(doc, dict):
            raise TypeError("agile_backlog.json root must be an object")
        return AgileBacklog(raw=doc)
