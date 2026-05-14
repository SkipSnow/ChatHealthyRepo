"""Builder for the brain artifact deployment_architecture.json.

Scans the live on-disk tree and produces a `DeploymentCollection` whose
`embedded_content` fields hold the verbatim source bytes of every
file that composes every deployment target. The output is a
production-grade collection; no stubs, no placeholders, no metadata
field anywhere in the data shape.

Binary files (anything whose bytes do not round-trip as UTF-8) are
base64-encoded and prefixed with `__base64__:` so the schema sees a
string and the Extractor can faithfully restore the raw bytes.
"""
from __future__ import annotations

import base64
from pathlib import Path

from target_record import (
    DeploymentCollection,
    EnvironmentBinding,
    FileComposition,
    TargetRecord,
)


_FEATURE_ID: str = "EPIC-008-F-012"
_BASE64_PREFIX: str = "__base64__:"


def _read_content_as_schema_string(abs_path: Path) -> str:
    """Return the file content as a schema-compatible string.

    Try utf-8 strict decode; on failure base64-encode and prefix.
    Any I/O error raises.
    """
    raw = abs_path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _BASE64_PREFIX + base64.b64encode(raw).decode("ascii")
    if text.encode("utf-8") != raw:
        return _BASE64_PREFIX + base64.b64encode(raw).decode("ascii")
    return text


def _rel_posix(repo_root: Path, abs_path: Path) -> str:
    rel = abs_path.relative_to(repo_root)
    return rel.as_posix()


def _classify_website_handler(suffix_lower: str) -> str:
    if suffix_lower == ".json":
        return "json"
    if suffix_lower in (".html", ".css", ".svg", ".png"):
        return "static_asset"
    if suffix_lower == ".md":
        return "markdown"
    return "static_asset"


class Builder:
    """Builds a `DeploymentCollection` by scanning the live tree."""

    def __init__(self) -> None:
        self._unclassified: list[str] = []

    def unclassified(self) -> list[str]:
        return list(self._unclassified)

    def build_collection(self, repo_root: Path) -> DeploymentCollection:
        repo_root = repo_root.resolve()
        coll = DeploymentCollection()
        coll.add(self._build_workflow_runner(repo_root))
        coll.add(self._build_hf_space_findcare_backend(repo_root))
        coll.add(self._build_hf_space_evaluatecare_backend(repo_root))
        coll.add(self._build_hf_space_shared_services(repo_root))
        coll.add(self._build_cloudflare_pages_website(repo_root))
        return coll

    @staticmethod
    def _file(
        repo_root: Path,
        rel_path: str,
        handler_type: str,
    ) -> FileComposition:
        abs_path = repo_root / rel_path
        if not abs_path.is_file():
            raise FileNotFoundError(
                f"Builder expected file at {abs_path}; absent on disk"
            )
        return FileComposition(
            feature_id=_FEATURE_ID,
            source_location=rel_path,
            handler_type=handler_type,
            embedded_content=_read_content_as_schema_string(abs_path),
        )

    def _build_workflow_runner(self, repo_root: Path) -> TargetRecord:
        workflows_dir = repo_root / ".github" / "workflows"
        if not workflows_dir.is_dir():
            raise FileNotFoundError(
                f"workflows directory missing at {workflows_dir}"
            )
        files: list[FileComposition] = []
        for yml in sorted(workflows_dir.glob("*.yml")):
            files.append(
                self._file(repo_root, _rel_posix(repo_root, yml), "workflow_yaml")
            )
        for yaml_alt in sorted(workflows_dir.glob("*.yaml")):
            files.append(
                self._file(
                    repo_root, _rel_posix(repo_root, yaml_alt), "workflow_yaml"
                )
            )
        addr = "github.com/ChatHealthy/repo/actions"
        envs = [
            EnvironmentBinding(env_binding="local", node_address=addr),
            EnvironmentBinding(env_binding="dev", node_address=addr),
            EnvironmentBinding(env_binding="qa", node_address=addr),
            EnvironmentBinding(env_binding="prod", node_address=addr),
        ]
        return TargetRecord(
            target_id="target_workflow_runner_github_actions",
            target_kind="github_actions_workflow_runner",
            environments=envs,
            files=files,
        )

    def _build_hf_space_findcare_backend(
        self, repo_root: Path
    ) -> TargetRecord:
        envs = [
            EnvironmentBinding(
                env_binding="dev",
                node_address="dev.chathealthy.ai/findcare",
            ),
            EnvironmentBinding(
                env_binding="qa",
                node_address="qa.chathealthy.ai/findcare",
            ),
            EnvironmentBinding(
                env_binding="prod",
                node_address="chathealthy.ai/findcare",
            ),
        ]
        files = [
            self._file(repo_root, "DevOps/FindCareBackend/Dockerfile", "dockerfile"),
            self._file(
                repo_root,
                "DevOps/FindCareBackend/requirements.txt",
                "python_requirements",
            ),
            self._file(
                repo_root,
                "Code/ConversationalUX/FindCareChat/backend/app.py",
                "python_source",
            ),
            self._file(
                repo_root,
                "Code/Shared/ChatHealthyMongoUtilities.py",
                "python_source",
            ),
            self._file(
                repo_root,
                "Code/Shared/prompt_system_maker.py",
                "python_source",
            ),
        ]
        return TargetRecord(
            target_id="target_hf_space_findcare_backend",
            target_kind="hf_space",
            environments=envs,
            files=files,
        )

    def _build_hf_space_evaluatecare_backend(
        self, repo_root: Path
    ) -> TargetRecord:
        envs = [
            EnvironmentBinding(
                env_binding="dev",
                node_address="dev.chathealthy.ai/evaluatecare",
            ),
            EnvironmentBinding(
                env_binding="qa",
                node_address="qa.chathealthy.ai/evaluatecare",
            ),
            EnvironmentBinding(
                env_binding="prod",
                node_address="chathealthy.ai/evaluatecare",
            ),
        ]
        files = [
            self._file(repo_root, "evaluateCare/Code/Dockerfile", "dockerfile"),
            self._file(
                repo_root, "evaluateCare/Code/requirements.txt", "python_requirements"
            ),
            self._file(repo_root, "evaluateCare/Code/app.py", "python_source"),
        ]
        return TargetRecord(
            target_id="target_hf_space_evaluatecare_backend",
            target_kind="hf_space",
            environments=envs,
            files=files,
        )

    def _build_hf_space_shared_services(self, repo_root: Path) -> TargetRecord:
        envs = [
            EnvironmentBinding(
                env_binding="dev",
                node_address="dev.chathealthy.ai/sharedservices",
            ),
            EnvironmentBinding(
                env_binding="qa",
                node_address="qa.chathealthy.ai/sharedservices",
            ),
            EnvironmentBinding(
                env_binding="prod",
                node_address="chathealthy.ai/sharedservices",
            ),
        ]
        files = [
            self._file(repo_root, "sharedServices/Code/Dockerfile", "dockerfile"),
            self._file(
                repo_root,
                "sharedServices/Code/requirements.txt",
                "python_requirements",
            ),
            self._file(repo_root, "sharedServices/Code/app.py", "python_source"),
        ]
        return TargetRecord(
            target_id="target_hf_space_shared_services",
            target_kind="hf_space",
            environments=envs,
            files=files,
        )

    def _build_cloudflare_pages_website(
        self, repo_root: Path
    ) -> TargetRecord:
        envs = [
            EnvironmentBinding(env_binding="dev", node_address="dev.chathealthy.ai"),
            EnvironmentBinding(env_binding="qa", node_address="qa.chathealthy.ai"),
            EnvironmentBinding(env_binding="prod", node_address="chathealthy.ai"),
        ]
        website_root = repo_root / "Website"
        if not website_root.is_dir():
            raise FileNotFoundError(
                f"Website directory missing at {website_root}"
            )
        files: list[FileComposition] = []
        for abs_path in sorted(website_root.rglob("*")):
            if not abs_path.is_file():
                continue
            parts_lower = {p.lower() for p in abs_path.parts}
            if "__pycache__" in parts_lower:
                continue
            suffix_lower = abs_path.suffix.lower()
            if suffix_lower in (".pyc",):
                continue
            handler = _classify_website_handler(suffix_lower)
            rel = _rel_posix(repo_root, abs_path)
            files.append(
                FileComposition(
                    feature_id=_FEATURE_ID,
                    source_location=rel,
                    handler_type=handler,
                    embedded_content=_read_content_as_schema_string(abs_path),
                )
            )
        return TargetRecord(
            target_id="target_cloudflare_pages_website",
            target_kind="cloudflare_pages_project",
            environments=envs,
            files=files,
        )
