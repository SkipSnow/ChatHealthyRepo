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
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
def _ch_exc():
    """ChatHealthyException without assuming the library is installed.
    These modules run as bare scripts in the devops chain."""
    import sys as _s, pathlib as _p
    for _d in _p.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _l = _d / "ChatHealthyLib" / "src"
            if str(_l) not in _s.path:
                _s.path.insert(0, str(_l))
            break
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException



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

    _BROWSER_UA: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self, schema_uri: str | Path | None = None) -> None:
        """`schema_uri` is accepted and ignored.

        This used to open a schema off the local filesystem --
        Website/schemas/ChatHealthyAgileBacklogSchema.json -- while
        agile_backlog.json declares a canonical https URL. So the backlog
        was validated on every build and deploy against a file that could
        differ from the schema it claims to satisfy, and a local edit made
        the check agree with itself.

        A document is validated against the schema the document names.
        """
        self.schema_uri: str = ""
        self._schema: dict | None = None
        self._validator = None

    def _bind_declared_schema(self, doc: dict) -> None:
        import urllib.request
        from jsonschema import Draft202012Validator

        declared = doc.get("$schema") if isinstance(doc, dict) else None
        if not declared:
            raise ChatHealthyException(
            mode="value_error",
            component="agile_backlog",
            message="agile_backlog.json declares no $schema; a document that "
                    "does not name the schema it satisfies cannot be validated")
        req = urllib.request.Request(
            declared, headers={"User-Agent": self._BROWSER_UA}
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                if resp.status != 200:
                    raise ChatHealthyException(
            mode="runtime_error",
            component="agile_backlog",
            message=f"schema URL {declared} returned HTTP {resp.status}")
                self._schema = json.loads(resp.read().decode("utf-8"))
        except OSError as exc:
            raise ChatHealthyException(
            mode="runtime_error",
            component="agile_backlog",
            message=f"cannot fetch agile-backlog schema from {declared}: "
                    f"{type(exc).__name__}: {exc}. No local fallback allowed.",
            exception=exc) from exc
        self.schema_uri = declared
        self._validator = Draft202012Validator(self._schema)

    def load(self, path: Path) -> AgileBacklog:
        with path.open("r", encoding="utf-8") as f:
            doc = json.load(f)
        self._bind_declared_schema(doc)
        errors = sorted(
            self._validator.iter_errors(doc), key=lambda e: list(e.path)
        )
        if errors:
            joined = "; ".join(
                f"{list(e.path)}: {e.message}" for e in errors[:5]
            )
            raise ChatHealthyException(
            mode="value_error",
            component="agile_backlog",
            message=f"agile_backlog failed schema validation: {joined}")
        if not isinstance(doc, dict):
            raise ChatHealthyException(
            mode="type_error",
            component="agile_backlog",
            message="agile_backlog.json root must be an object")
        return AgileBacklog(raw=doc)
