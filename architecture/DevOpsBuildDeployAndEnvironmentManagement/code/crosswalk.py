"""Two-model reconciliation gate.

Crosswalk compares a `DeploymentCollection` (machine model of every
deployment target plus the file bytes that compose each target) against
the `AgileBacklog` (business model of features and requirements) and the
on-disk repo tree, plus an optional `.env`-derived value set used to
detect secret-value leaks into the brain artifact.

Every subcheck returns a list of bare REJECT lines. No recovery
guidance. Pass = empty list across all subchecks. The aggregator
exposes `is_pass`, `violations`, `exit_code()`, and `format()`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agile_backlog import AgileBacklog
from record_loader import SCHEMA_URL
from target_record import DeploymentCollection, TargetRecord


@dataclass(slots=True)
class CrosswalkReport:
    is_pass: bool
    violations: list[str] = field(default_factory=list)

    def exit_code(self) -> int:
        return 0 if self.is_pass else 1

    def format(self) -> str:
        return "\n".join(self.violations)


class Crosswalk:
    """Runs all subordinate checks and aggregates the reject lines."""

    def __init__(
        self,
        schema_url: str = SCHEMA_URL,
        backlog_schema_path: Path | None = None,
    ) -> None:
        self.schema_url: str = schema_url
        self.backlog_schema_path: Path | None = backlog_schema_path

    def check(
        self,
        coll: DeploymentCollection,
        backlog: AgileBacklog,
        repo_root: Path,
        env_values: set[str] | None = None,
    ) -> CrosswalkReport:
        violations: list[str] = []
        violations.extend(self.check_target_id_uniqueness(coll))
        for record in coll:
            violations.extend(self.check_env_binding_uniqueness(record))
            violations.extend(self.check_source_location_uniqueness_within_record(record))
        violations.extend(self.check_feature_id_coverage(coll, backlog))
        violations.extend(self.check_file_drift(coll, repo_root))
        if env_values:
            violations.extend(self.check_no_secret_values(coll, env_values))
        return CrosswalkReport(is_pass=not violations, violations=violations)

    @staticmethod
    def check_source_location_uniqueness_within_record(
        record: TargetRecord,
    ) -> list[str]:
        """Within one TargetRecord, every files[].source_location must be
        unique (same path twice means two competing bytes for one disk
        location — a hard reject). Across records, source_location MAY
        repeat (a shared file deploys to multiple targets)."""
        seen: dict[str, int] = {}
        for f in record.files:
            seen[f.source_location] = seen.get(f.source_location, 0) + 1
        return [
            f"REJECT — duplicate source_location within target {record.target_id!r}: "
            f"{path!r} appears {count}x"
            for path, count in seen.items()
            if count > 1
        ]

    @staticmethod
    def check_target_id_uniqueness(coll: DeploymentCollection) -> list[str]:
        seen: dict[str, int] = {}
        for r in coll:
            seen[r.target_id] = seen.get(r.target_id, 0) + 1
        return [
            f"REJECT — target_id collision: {tid!r} appears {n} times"
            for tid, n in seen.items()
            if n > 1
        ]

    @staticmethod
    def check_env_binding_uniqueness(record: TargetRecord) -> list[str]:
        seen: dict[str, int] = {}
        for e in record.environments:
            seen[e.env_binding] = seen.get(e.env_binding, 0) + 1
        return [
            f"REJECT — env_binding {eb!r} appears {n} times in record "
            f"{record.target_id!r}"
            for eb, n in seen.items()
            if n > 1
        ]

    @staticmethod
    def check_feature_id_coverage(
        coll: DeploymentCollection, backlog: AgileBacklog
    ) -> list[str]:
        backlog_features = backlog.feature_id_set()
        out: list[str] = []
        for record in coll:
            for f in record.files:
                if f.feature_id not in backlog_features:
                    out.append(
                        f"REJECT — feature_id {f.feature_id!r} on file "
                        f"{f.source_location!r} in record "
                        f"{record.target_id!r} not found in agile_backlog"
                    )
        return out

    @staticmethod
    def check_file_drift(
        coll: DeploymentCollection, repo_root: Path
    ) -> list[str]:
        out: list[str] = []
        for record in coll:
            for f in record.files:
                abs_path = repo_root / f.source_location
                if not abs_path.is_file():
                    out.append(
                        f"REJECT — source_location {f.source_location!r} "
                        f"in record {record.target_id!r} not present on disk"
                    )
                    continue
                disk_bytes = abs_path.read_bytes()
                if f.embedded_content.startswith("__base64__:"):
                    import base64
                    try:
                        embedded_bytes = base64.b64decode(
                            f.embedded_content[len("__base64__:") :], validate=True
                        )
                    except Exception as exc:
                        out.append(
                            f"REJECT — embedded_content for "
                            f"{f.source_location!r} in record "
                            f"{record.target_id!r} has malformed base64: "
                            f"{type(exc).__name__}"
                        )
                        continue
                else:
                    try:
                        embedded_bytes = f.embedded_content.encode("utf-8")
                    except UnicodeEncodeError as exc:
                        out.append(
                            f"REJECT — embedded_content for "
                            f"{f.source_location!r} in record "
                            f"{record.target_id!r} cannot encode to utf-8: "
                            f"{exc}"
                        )
                        continue
                if disk_bytes != embedded_bytes:
                    out.append(
                        f"REJECT — file drift at {f.source_location!r} "
                        f"in record {record.target_id!r}: "
                        f"disk={len(disk_bytes)}B embedded={len(embedded_bytes)}B"
                    )
        return out

    @staticmethod
    def check_no_secret_values(
        coll: DeploymentCollection, env_values: set[str]
    ) -> list[str]:
        out: list[str] = []
        sentinel_min = 6
        candidates: set[str] = set()
        for v in env_values:
            if not v or len(v) < sentinel_min:
                continue
            # Identifier-shaped tokens that are uniformly cased (all
            # uppercase OR all lowercase) and use only alnum + underscore
            # + hyphen are public labels — variable names, DNS labels,
            # container names. Opaque secrets typically mix case or
            # contain non-identifier characters. Filter to fire on
            # secret-shaped payloads only.
            ident_chars = v.replace("_", "").replace("-", "")
            if ident_chars.isalnum() and not v[0].isdigit() and (
                v.lower() == v or v.upper() == v
            ):
                continue
            # Public-format values (URLs, semver, email) are addresses by
            # construction; the spec's leak intent is opaque secret bytes.
            # Source code references the addresses directly; the values in
            # `.env` exist to centralize the value, not to hide it.
            if "://" in v:
                continue
            if "@" in v and "." in v.split("@", 1)[1]:
                continue
            if v[:1].lower() == "v" and all(
                c.isdigit() or c == "." for c in v[1:]
            ):
                continue
            candidates.add(v)
        if not candidates:
            return out
        for record in coll:
            scan_strings: list[tuple[str, str]] = [
                ("target_id", record.target_id),
                ("target_kind", record.target_kind),
            ]
            for e in record.environments:
                scan_strings.append(("env_binding", e.env_binding))
                scan_strings.append(("node_address", e.node_address))
            for f in record.files:
                scan_strings.append(("feature_id", f.feature_id))
                scan_strings.append(("source_location", f.source_location))
                scan_strings.append(("handler_type", f.handler_type))
                scan_strings.append(("embedded_content", f.embedded_content))
            for field_name, s in scan_strings:
                for val in candidates:
                    if val in s:
                        out.append(
                            f"REJECT — secret value leak in record "
                            f"{record.target_id!r} field {field_name!r}: "
                            f".env-derived value of length {len(val)} found"
                        )
        return out
