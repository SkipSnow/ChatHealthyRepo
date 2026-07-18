"""Schema-validated reader for the deployment-architecture JSON.

Reads `brain/machine_artifacts/content/deployment_architecture.json` (the
canonical brain artifact: a JSON array of TargetRecord objects) and
returns a typed `DeploymentCollection`. Every load schema-validates;
absent / empty / malformed JSON raises hard.

Schema resolution policy:
  - Brain-artifact schemas (URLs under *.chathealthy.ai/schemas/) are
    read from the local repo at Website/schemas/<basename>. The
    deployment_architecture.json schema is a repo-internal artifact used
    only by our own build/deploy/pre-commit code; the URL exists only for
    IDE convenience. The git working tree is the source of truth.
  - Any other URL is HTTP-fetched (external schemas we do not own).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import jsonschema

from target_record import DeploymentCollection, TargetRecord

SCHEMA_URL: str = (
    "https://dev.chathealthy.ai/schemas/ChatHealthyDeploymentArchitectureSchema.json"
)


def _repo_root() -> Path:
    """Locate the repo root by walking up from this file."""
    p = Path(__file__).resolve()
    for candidate in [p, *p.parents]:
        if (candidate / "brain" / "machine_artifacts" / "content").is_dir():
            return candidate
    raise RuntimeError(
        f"cannot locate repo root from {Path(__file__).resolve()}"
    )


def _local_path_for_chathealthy_schema_url(schema_url: str) -> Path | None:
    """If schema_url is a chathealthy.ai schema URL, return the local
    repo path that mirrors it. Otherwise return None."""
    parsed = urllib.parse.urlparse(schema_url)
    host = (parsed.hostname or "").lower()
    if not host.endswith("chathealthy.ai"):
        return None
    path = parsed.path or ""
    if "/schemas/" not in path:
        return None
    basename = path.rsplit("/", 1)[-1]
    if not basename.endswith(".json"):
        return None
    return _repo_root() / "Website" / "schemas" / basename


class RecordLoader:
    """Loads a `DeploymentCollection` from the canonical brain JSON.

    Every load schema-validates. Brain-artifact schemas resolve to the
    local Website/schemas/<basename> file; external schemas HTTP-fetch.
    """

    # Cloudflare-fronted hosts return 403 to Python's default User-Agent
    # ("Python-urllib/X.Y"). Kept for the external-fetch path.
    _BROWSER_UA: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self, schema_url: str = SCHEMA_URL, *, timeout: float = 10.0) -> None:
        self.schema_url: str = schema_url
        import os as _os
        local_override = _os.environ.get("CHATHEALTHY_LOCAL_SCHEMA_PATH", "").strip()
        if local_override:
            with open(local_override, "r", encoding="utf-8") as f:
                self._schema = json.load(f)
        else:
            local_path = _local_path_for_chathealthy_schema_url(schema_url)
            if local_path is not None:
                if not local_path.is_file():
                    raise RuntimeError(
                        f"expected local schema at {local_path} for URL "
                        f"{schema_url} — brain-artifact schemas live in the "
                        f"repo tree, not on the network."
                    )
                self._schema = json.loads(local_path.read_text(encoding="utf-8"))
            else:
                # External schema we don't own — HTTP fetch as before.
                req = urllib.request.Request(
                    schema_url, headers={"User-Agent": self._BROWSER_UA}
                )
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        if resp.status != 200:
                            raise RuntimeError(
                                f"schema URL {schema_url} returned HTTP {resp.status}"
                            )
                        self._schema = json.loads(resp.read().decode("utf-8"))
                except urllib.error.URLError as exc:
                    raise RuntimeError(
                        f"cannot fetch external schema from {schema_url}: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
        self._validator = jsonschema.Draft202012Validator(self._schema)

    def _validate(self, doc: dict) -> None:
        errors = sorted(
            self._validator.iter_errors(doc), key=lambda e: list(e.path)
        )
        if errors:
            joined = "; ".join(
                f"{list(e.path)}: {e.message}" for e in errors[:5]
            )
            raise ValueError(f"TargetRecord failed schema validation: {joined}")

    def load(self, path: Path) -> TargetRecord:
        if not path.is_file():
            raise FileNotFoundError(f"TargetRecord JSON not found at {path}")
        with path.open("r", encoding="utf-8") as f:
            doc = json.load(f)
        if not isinstance(doc, dict):
            raise TypeError(
                f"TargetRecord JSON at {path} must be an object, got {type(doc).__name__}"
            )
        self._validate(doc)
        return TargetRecord.from_dict(doc)

    def load_collection(self, path: str | Path) -> DeploymentCollection:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(
                f"deployment_architecture.json not found at {p}. "
                f"The brain artifact is the source of record; absence is a hard reject."
            )
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # The on-disk shape is an envelope:
        # {"$schema": "...", "DeploymentTargetRecord": [TargetRecord, ...]}.
        # The envelope satisfies Rule-008 by carrying a top-level $schema
        # field; on import to MongoDB the envelope dissolves and each
        # entry in `DeploymentTargetRecord[]` becomes one document.
        if not isinstance(data, dict):
            raise TypeError(
                f"{p} must contain a JSON object envelope with a "
                f"'DeploymentTargetRecord' array, got {type(data).__name__}"
            )
        records = data.get("DeploymentTargetRecord")
        if not isinstance(records, list):
            raise TypeError(
                f"{p} 'DeploymentTargetRecord' field must be a JSON array of "
                f"TargetRecord objects, got {type(records).__name__}"
            )
        if not records:
            raise ValueError(
                f"{p} DeploymentTargetRecord array is empty; the deployment "
                f"record collection has no entries. This is a hard reject."
            )
        # Validate the WHOLE envelope against the schema in one pass. The
        # schema describes the envelope shape and its DeploymentTargetRecord
        # sub-schema validates each record. Per-record validation against
        # the envelope schema would fail (each record lacks the envelope
        # top-level $schema/DeploymentTargetRecord fields).
        self._validate(data)
        # Use DeploymentCollection.from_list so package expansion is
        # applied consistently.
        coll = DeploymentCollection.from_list(records)
        # Preserve the top-level IdentityCatalog + CustomRoleCatalog so
        # the deploy chain can iterate identity/role facts from the
        # manifest instead of from hardcoded scripts.
        coll.identity_catalog = list(data.get("IdentityCatalog", []) or [])
        coll.custom_role_catalog = list(data.get("CustomRoleCatalog", []) or [])
        return coll
