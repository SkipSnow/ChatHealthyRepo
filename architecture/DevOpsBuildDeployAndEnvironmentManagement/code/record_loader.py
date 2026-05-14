"""Schema-validated reader for the deployment-architecture JSON.

Reads `brain/machine_artifacts/content/deployment_architecture.json` (the
canonical brain artifact: a JSON array of TargetRecord objects) and
returns a typed `DeploymentCollection`. Every load schema-validates;
absent / empty / malformed JSON raises hard.

The schema is the canonical web URL:
  https://dev.chathealthy.ai/schemas/ChatHealthyDeploymentArchitectureSchema.json
Per the operator's no-local-fallback rule, the loader fetches the schema
over HTTPS. A filesystem fallback is forbidden. Network failure raises.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import jsonschema

from target_record import DeploymentCollection, TargetRecord

SCHEMA_URL: str = (
    "https://dev.chathealthy.ai/schemas/ChatHealthyDeploymentArchitectureSchema.json"
)


class RecordLoader:
    """Loads a `DeploymentCollection` from the canonical brain JSON.

    The loader fetches the schema via HTTPS at construction time (no
    filesystem fallback). Every load schema-validates each record in the
    collection. Missing or empty collection JSON raises.
    """

    # Cloudflare-fronted hosts return 403 to Python's default User-Agent
    # ("Python-urllib/X.Y"). We supply a generic browser UA on every schema
    # fetch from within the loader so callers do not have to install a
    # global opener. This is HTTP-client housekeeping; the schema source
    # is still the canonical HTTPS URL with no filesystem fallback.
    _BROWSER_UA: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self, schema_url: str = SCHEMA_URL, *, timeout: float = 10.0) -> None:
        self.schema_url: str = schema_url
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
                f"cannot fetch deployment-architecture schema from {schema_url}: "
                f"{type(exc).__name__}: {exc}. No local fallback allowed."
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
        coll = DeploymentCollection()
        for doc in records:
            coll.add(TargetRecord.from_dict(doc))
        return coll
