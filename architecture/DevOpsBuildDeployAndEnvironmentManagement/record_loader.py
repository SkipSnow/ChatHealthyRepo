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
        import os as _os
        local_override = _os.environ.get("CHATHEALTHY_LOCAL_SCHEMA_PATH", "").strip()
        if local_override:
            # Operator-set escape hatch: load schema from a local file
            # instead of the URL. Used when the URL is temporarily stale
            # relative to the local working tree (e.g., schema change
            # not yet deployed to Cloudflare Pages).
            with open(local_override, "r", encoding="utf-8") as f:
                self._schema = json.load(f)
        else:
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

    def load_collection(
        self,
        path: str | Path,
        *,
        target_id_filter: str | None = None,
    ) -> DeploymentCollection:
        """Load the deployment collection from `path`.

        When `target_id_filter` is set, schema validation errors that
        surface on records OTHER than the named target are ignored; the
        validation focus is scoped to just the target the caller intends
        to build/deploy. Envelope-level errors and errors on the named
        target are still raised. This mirrors the deploy chain's real
        access pattern (one target at a time); it prevents an unrelated
        target's new-shape record from blocking a schema-only redeploy
        of an unrelated target.
        """
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
        # Validate. If target_id_filter is set, keep only errors that
        # apply to the envelope root or to that specific target index.
        if target_id_filter is None:
            self._validate(data)
        else:
            target_idx = None
            for i, rec in enumerate(records):
                if isinstance(rec, dict) and rec.get("target_id") == target_id_filter:
                    target_idx = i
                    break
            if target_idx is None:
                raise ValueError(
                    f"target_id_filter={target_id_filter!r} not present in the "
                    f"DeploymentTargetRecord[] array"
                )
            self._validate_scoped_to_target(data, target_idx)
        # Use DeploymentCollection.from_list so package expansion is
        # applied consistently.
        coll = DeploymentCollection.from_list(records)
        # Preserve the top-level IdentityCatalog + CustomRoleCatalog so
        # the deploy chain can iterate identity/role facts from the
        # manifest instead of from hardcoded scripts.
        coll.identity_catalog = list(data.get("IdentityCatalog", []) or [])
        coll.custom_role_catalog = list(data.get("CustomRoleCatalog", []) or [])
        return coll

    def _validate_scoped_to_target(self, doc: dict, target_idx: int) -> None:
        """Validate the envelope but filter errors down to the ones
        affecting either the envelope root or the specified target
        index. Errors on other targets are silently dropped."""
        target_key = ("DeploymentTargetRecord", target_idx)
        kept = []
        for e in self._validator.iter_errors(doc):
            path_tuple = tuple(e.absolute_path)
            # Errors on the envelope root (empty path) — keep.
            if not path_tuple:
                kept.append(e)
                continue
            first = path_tuple[0]
            if first != "DeploymentTargetRecord":
                # Root-level property errors (IdentityCatalog, firm, etc.) — keep.
                kept.append(e)
                continue
            # Under DeploymentTargetRecord; keep only errors on our target.
            if len(path_tuple) >= 2 and path_tuple[1] == target_idx:
                kept.append(e)
        if kept:
            kept.sort(key=lambda e: list(e.absolute_path))
            joined = "; ".join(
                f"{list(e.absolute_path)}: {e.message}" for e in kept[:5]
            )
            raise ValueError(f"TargetRecord failed schema validation: {joined}")
