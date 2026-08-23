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
import sys
import urllib.error
import urllib.request
from pathlib import Path

import jsonschema

from target_record import DeploymentCollection, TargetRecord

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "ChatHealthyLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
# The chain materialises the application .env, which sets
# CH_LOG_DESTINATION=mongo and CH_LOG_DB=pipelineAdmin. Those are the
# deployed application's facts, not this tool's: devops tooling runs on
# a workstation and its log is the operator's terminal. Inheriting them
# made a build depend on a Mongo write it has no grant for.
import os as _ch_os
_ch_os.environ["CH_LOG_DESTINATION"] = "stderr"
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_CH_LOG = ChatHealthyLoggingService()

ARCHITECTURE_REL = Path(
    "brain/machine_artifacts/content/deployment_architecture.json"
)

SCHEMA_URL: str = (
    "https://dev.chathealthy.ai/schemas/ChatHealthyDeploymentArchitectureSchema.json"
)



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
        # The published schema is the only schema. There was an environment
        # variable here that loaded one off the local filesystem instead,
        # for use "when the URL is temporarily stale relative to the working
        # tree" -- which is precisely the situation the schema-change
        # sequence exists to prevent, and precisely when it must not be
        # skipped. It made every gate it touched optional: a manifest that
        # only the local file accepted validated clean, and the build
        # reported success against a schema nothing else in the firm had.
        # A schema change ships before the change that needs it. There is
        # no path around that.
        req = urllib.request.Request(
            schema_url, headers={"User-Agent": self._BROWSER_UA}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise ChatHealthyException(
            mode="runtime_error",
            component="record_loader",
            message=f"schema URL {schema_url} returned HTTP {resp.status}")
                self._schema = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise ChatHealthyException(
            mode="runtime_error",
            component="record_loader",
            message=f"cannot fetch deployment-architecture schema from {schema_url}: "
                    f"{type(exc).__name__}: {exc}. No local fallback allowed.",
            exception=exc) from exc
        self._validator = jsonschema.Draft202012Validator(self._schema)

    def _rebind_to_declared_schema(self, doc: dict, source: Path) -> None:
        """Point this loader at the schema the document names."""
        declared = doc.get("$schema")
        if not declared:
            raise ChatHealthyException(
            mode="value_error",
            component="record_loader",
            message=f"{source} declares no $schema; a document that does not "
                    "name the schema it satisfies cannot be validated")
        if declared == self.schema_url and self._schema is not None:
            return
        req = urllib.request.Request(
            declared, headers={"User-Agent": self._BROWSER_UA}
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                if resp.status != 200:
                    raise ChatHealthyException(
            mode="runtime_error",
            component="record_loader",
            message=f"schema URL {declared} returned HTTP {resp.status}")
                self._schema = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise ChatHealthyException(
            mode="runtime_error",
            component="record_loader",
            message=f"cannot fetch schema from {declared}: "
                    f"{type(exc).__name__}: {exc}. No local fallback allowed.",
            exception=exc) from exc
        self.schema_url = declared
        self._validator = jsonschema.Draft202012Validator(self._schema)

    @classmethod
    def validate_architecture(cls, repo_root: Path) -> None:
        """Validate the whole manifest against the published schema. Abend.

        Runs before anything else in both build and deploy, and before any
        step that writes to the manifest -- the build used to refresh
        content hashes into the file and validate the result, so the thing
        being checked was already something the build had changed.

        No partial pass: one error stops the run. A manifest that does not
        satisfy the published schema is not a manifest anything should be
        built from or deployed from.
        """
        path = repo_root / ARCHITECTURE_REL
        if not path.is_file():
            raise ChatHealthyException(
                mode="aborted",
                component="record_loader",
                message=f"ABEND: {path} not found.",
            exit_code=2)
        doc = json.loads(path.read_text(encoding="utf-8"))
        # The document names the schema it claims to satisfy. Validate
        # against that, not against a URL held in this module -- a constant
        # here can silently disagree with what the file declares, and then
        # the check passes against a schema the document never claimed.
        declared = doc.get("$schema")
        if not declared:
            raise ChatHealthyException(
                mode="aborted",
                component="record_loader",
                message=f"ABEND: {path} declares no $schema. A document that does "
                f"not name the schema it satisfies cannot be validated.",
            exit_code=2)
        loader = cls(declared)
        errors = sorted(
            loader._validator.iter_errors(doc),
            key=lambda e: list(map(str, e.absolute_path)),
        )
        if errors:
            _CH_LOG.error(f"ABEND: deployment_architecture.json fails the published "
                f"schema at {loader.schema_url}\n"
                f"       {len(errors)} error(s):")
            for err in errors[:20]:
                where = "/".join(str(p) for p in err.absolute_path) or "<root>"
                _CH_LOG.error(f"         {where}: {err.message}")
            if len(errors) > 20:
                _CH_LOG.error(f"         ... and {len(errors) - 20} more")
            raise ChatHealthyException(
                mode="aborted",
                component="record_loader",
                message="       A schema change ships before the manifest change "
                "that needs it.",
            exit_code=2)
        _CH_LOG.info(f"[schema] deployment_architecture.json validates against "
              f"{loader.schema_url}")

    def _validate(self, doc: dict) -> None:
        errors = sorted(
            self._validator.iter_errors(doc), key=lambda e: list(e.path)
        )
        if errors:
            joined = "; ".join(
                f"{list(e.path)}: {e.message}" for e in errors[:5]
            )
            raise ChatHealthyException(
            mode="value_error",
            component="record_loader",
            message=f"TargetRecord failed schema validation: {joined}")

    def load(self, path: Path) -> TargetRecord:
        if not path.is_file():
            raise ChatHealthyException(
            mode="file_missing",
            component="record_loader",
            message=f"TargetRecord JSON not found at {path}")
        with path.open("r", encoding="utf-8") as f:
            doc = json.load(f)
        if not isinstance(doc, dict):
            raise ChatHealthyException(
            mode="type_error",
            component="record_loader",
            message=f"TargetRecord JSON at {path} must be an object, got {type(doc).__name__}")
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
            raise ChatHealthyException(
            mode="file_missing",
            component="record_loader",
            message=f"deployment_architecture.json not found at {p}. "
                f"The brain artifact is the source of record; absence is a hard reject.")
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # The on-disk shape is an envelope:
        # {"$schema": "...", "DeploymentTargetRecord": [TargetRecord, ...]}.
        # The envelope satisfies Rule-008 by carrying a top-level $schema
        # field; on import to MongoDB the envelope dissolves and each
        # entry in `DeploymentTargetRecord[]` becomes one document.
        if not isinstance(data, dict):
            raise ChatHealthyException(
            mode="type_error",
            component="record_loader",
            message=f"{p} must contain a JSON object envelope with a "
                f"'DeploymentTargetRecord' array, got {type(data).__name__}")
        # Validate against the schema THIS document names, not the module
        # constant the loader was constructed with. They are the same string
        # today, which is exactly why the difference goes unnoticed: a
        # document that declared something else would still be checked
        # against the constant and would pass a schema it never claimed.
        self._rebind_to_declared_schema(data, p)
        records = data.get("DeploymentTargetRecord")
        if not isinstance(records, list):
            raise ChatHealthyException(
            mode="type_error",
            component="record_loader",
            message=f"{p} 'DeploymentTargetRecord' field must be a JSON array of "
                f"TargetRecord objects, got {type(records).__name__}")
        if not records:
            raise ChatHealthyException(
            mode="value_error",
            component="record_loader",
            message=f"{p} DeploymentTargetRecord array is empty; the deployment "
                f"record collection has no entries. This is a hard reject.")
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
                raise ChatHealthyException(
            mode="value_error",
            component="record_loader",
            message=f"target_id_filter={target_id_filter!r} not present in the "
                    f"DeploymentTargetRecord[] array")
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
            raise ChatHealthyException(
            mode="value_error",
            component="record_loader",
            message=f"TargetRecord failed schema validation: {joined}")
