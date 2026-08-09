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
        """disposition='managed' → byte-equality with disk.
        disposition='referenced' → presence-on-disk only.
        """
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
                if f.disposition == "referenced":
                    # Presence verified above. If the record carries a
                    # content_hash, the disk bytes must still hash to it.
                    # Builder populates content_hash on every run, so a
                    # mismatch means disk drifted from the committed/JSON
                    # state — typically an unstaged edit that the next
                    # deploy would ship without (the local Docker would
                    # work from disk, the remote build from commit, and
                    # diverge silently).
                    if f.content_hash is not None:
                        import hashlib as _hashlib
                        # CRLF→LF normalize to match Builder, so Windows
                        # local and Linux CI compute the same value.
                        raw = abs_path.read_bytes().replace(b"\r\n", b"\n")
                        disk_hash = _hashlib.sha256(raw).hexdigest()
                        if disk_hash != f.content_hash:
                            out.append(
                                f"REJECT — content_hash drift on referenced "
                                f"{f.source_location!r} in record "
                                f"{record.target_id!r}: disk={disk_hash[:12]}... "
                                f"json={f.content_hash[:12]}..."
                            )
                    continue
                if f.disposition != "managed":
                    out.append(
                        f"REJECT — unknown disposition {f.disposition!r} "
                        f"for {f.source_location!r} in record "
                        f"{record.target_id!r}"
                    )
                    continue
                # managed: byte-equality required.
                if f.embedded_content is None:
                    out.append(
                        f"REJECT — managed file {f.source_location!r} in "
                        f"record {record.target_id!r} missing embedded_content"
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
                if disk_bytes == embedded_bytes:
                    continue
                # If the only difference is line endings (CRLF vs LF on
                # either side), this is a Windows/Linux representation
                # artifact, not a real content change. Linux Docker and
                # text-handling tools accept both. Skip the byte-equality
                # check with an explanatory notice so operators see the
                # skip on the screen and know why the deploy proceeded.
                if (disk_bytes.replace(b"\r\n", b"\n")
                        == embedded_bytes.replace(b"\r\n", b"\n")):
                    print(
                        f"[crosswalk] SKIP byte-equality on "
                        f"{f.source_location!r} (record {record.target_id!r}): "
                        f"line-ending-only difference "
                        f"(disk={len(disk_bytes)}B embedded={len(embedded_bytes)}B). "
                        f"Content is identical after CRLF/LF normalization; "
                        f"safe to deploy."
                    )
                    continue
                # Real content drift.
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
        # env_values is already the authority: SecretsResolver returns the
        # values of the keys declared under `# Secrets` in .env, and nothing
        # else. Every one of them is scanned.
        #
        # There was a filter here that dropped needles whose VALUE looked
        # like a public label -- identifier-shaped, uniformly cased, not
        # leading with a digit. It could not work, because what a secret is
        # cannot be read off its bytes. On our own data it was wrong in both
        # directions at once: it dropped ATLAS_PRIVATE_KEY, a real
        # credential, for beginning with a letter, while failing to drop the
        # Azure subscription id it was added to suppress, which begins with
        # a digit. For a GUID the whole test reduced to the first hex
        # character. The declaration decides; the value never does.
        #
        # It existed because seven identifiers were misfiled as secrets, and
        # this suppressed the resulting false rejects. They are now declared
        # `# SecretSafe`, which is the fix the filter was standing in for.
        out: list[str] = []
        sentinel_min = 6
        candidates = {v for v in env_values if v and len(v) >= sentinel_min}
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
                if f.embedded_content is not None:
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
