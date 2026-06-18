"""Secret value resolver and leak-check utility.

Single value-fetching code path per binding: a name is bound to one store
and only that store is consulted. No cross-store fallback. Unavailable
store raises hard. Today only the local `.env` store is wired; the
remote stores (GitHub Actions secrets, Hugging Face Space secrets,
Cloudflare environment variables) are intentional stubs raising
NotImplementedError so the architectural shape is in place without
exposing fetch logic for the remote stores.

The leak-check helper exposes the set of values present in a local
`.env` so the Crosswalk can guarantee no `.env` value bytes ever leak
into the brain artifact's embedded_content (or any other string field).
The helper NEVER logs or writes the values it reads.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from target_record import DeploymentCollection


_STORE_LOCAL_ENV: str = "local_env"
_STORE_GHA: str = "gha_secret"
_STORE_HF_SPACE: str = "hf_space_secret"
_STORE_CLOUDFLARE: str = "cloudflare_env"
_STORE_AZURE_FA: str = "azure_function_app_setting"
_STORE_AZURE_AA: str = "azure_automation_variable"
_STORE_AZURE_AA_WEBHOOK: str = "azure_automation_webhook"


class SecretsResolver:
    """Resolve a bound secret to a value via the single bound store.

    Construction takes a mapping `(secret_name, env) -> store_id` so the
    binding is explicit. `resolve` looks up the bound store and reads
    the value from that store only — no fallback. Today only
    `local_env` is wired.
    """

    def __init__(
        self,
        bindings: dict[tuple[str, str], str] | None = None,
        env_file: Path | None = None,
    ) -> None:
        self._bindings: dict[tuple[str, str], str] = dict(bindings or {})
        self._env_file: Path | None = env_file
        self._env_cache: dict[str, str] | None = None

    @classmethod
    def from_collection(
        cls,
        coll: "DeploymentCollection",
        env_file: Path | None = None,
    ) -> "SecretsResolver":
        """Build bindings by reading per-target `secrets` declarations.

        Per EPIC-008-F-012-S-001-REQ-T-038: bindings dict MUST be
        constructed by reading per-target key declarations from the
        manifest. Each TargetRecord enumerates its own keys; each
        (name, env_binding) pair in any target maps to that target's
        bound store_id. If two targets declare the same (name, env)
        with different store ids, that is a hairball — raise.
        """
        bindings: dict[tuple[str, str], str] = {}
        _STORE_IDS = {
            _STORE_LOCAL_ENV, _STORE_GHA, _STORE_HF_SPACE, _STORE_CLOUDFLARE,
            _STORE_AZURE_FA, _STORE_AZURE_AA, _STORE_AZURE_AA_WEBHOOK,
        }
        for record in coll:
            envs = [e.env_binding for e in record.environments]
            # Iterate BOTH secrets and variables: any entry whose qualifier
            # is a known store_id participates in resolver binding (the
            # qualifier syntax for variables also supports peer_url:, env_name,
            # local_cert_file:, rename_from:, literal: — those are resolved
            # at deploy time without going through this registry).
            for block in (record.secrets, record.variables):
                if not block:
                    continue
                for name, store_id in block.items():
                    if store_id not in _STORE_IDS:
                        continue
                    for env in envs:
                        key = (name, env)
                        existing = bindings.get(key)
                        if existing is not None and existing != store_id:
                            raise ValueError(
                                f"binding conflict for {key!r}: "
                                f"target {record.target_id!r} declares "
                                f"{store_id!r}, prior target declared {existing!r}"
                            )
                        bindings[key] = store_id
        return cls(bindings=bindings, env_file=env_file)

    def resolve(self, name: str, env: str) -> str:
        key = (name, env)
        store = self._bindings.get(key)
        if store is None:
            raise KeyError(
                f"no binding registered for secret {name!r} in env {env!r}"
            )
        if store == _STORE_LOCAL_ENV:
            if self._env_file is None:
                raise RuntimeError(
                    f"binding {key!r} maps to {_STORE_LOCAL_ENV!r} "
                    "but no env_file was provided at construction"
                )
            if self._env_cache is None:
                self._env_cache = self._read_env_file(self._env_file)
            if name not in self._env_cache:
                raise KeyError(
                    f"secret {name!r} not present in {self._env_file}"
                )
            return self._env_cache[name]
        if store == _STORE_GHA:
            self._read_gha_secret_store(env)
        if store == _STORE_HF_SPACE:
            self._read_hf_space_secrets(env)
        if store == _STORE_CLOUDFLARE:
            self._read_cloudflare_env_vars(env)
        if store == _STORE_AZURE_FA:
            self._read_azure_function_app_settings(env)
        if store == _STORE_AZURE_AA:
            self._read_azure_automation_variables(env)
        if store == _STORE_AZURE_AA_WEBHOOK:
            raise RuntimeError(
                f"binding {key!r} maps to {_STORE_AZURE_AA_WEBHOOK!r}; "
                f"this store is NEVER resolved through SecretsResolver. "
                f"The upstream azure_automation_runbook target's deploy "
                f"step mints/reuses the webhook and pushes the URL onto "
                f"the consumer target via UPSERT. The consumer's own "
                f"deploy handler MUST skip secrets[] entries carrying "
                f"this store before calling resolve()."
            )
        raise RuntimeError(
            f"binding {key!r} maps to unknown store {store!r}; no fallback"
        )

    def env_values_for_leak_check(self, env_file: Path) -> set[str]:
        """Return the set of secret VALUES the leak-check must guard.

        Per EPIC-008-F-012-S-001-REQ-T-057 the `.env` is organized into
        two top-level sections, `# Secrets` and `# SecretSafe`. Only
        values whose key falls under `# Secrets` enter the needle set;
        SecretSafe values (URLs, model names, identifiers, booleans) are
        substring-matched freely in build-package files and never trip
        the leak-check.

        Fails loud if the `# Secrets` header is missing or no keys fall
        under it. Nothing from this set is ever logged or persisted.
        """
        secrets = self._read_env_secrets(env_file)
        return {v for v in secrets.values() if v}

    @staticmethod
    def _read_env_secrets(path: Path) -> dict[str, str]:
        """Parse a `.env` and return only the `# Secrets` section's keys.

        Recognizes two top-level section headers exactly: `# Secrets`
        and `# SecretSafe`. Any other `# …` line (or `## …`) is a
        comment / sub-header and does not change the active section.
        Keys outside `# Secrets` are excluded from the result.
        """
        result: dict[str, str] = {}
        section: str | None = None
        with path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.rstrip("\r\n")
                stripped = line.strip()
                if stripped == "# Secrets":
                    section = "Secrets"
                    continue
                if stripped == "# SecretSafe":
                    section = "SecretSafe"
                    continue
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" not in stripped:
                    continue
                if section != "Secrets":
                    continue
                key, _, val = stripped.partition("=")
                key = key.strip()
                if not key:
                    continue
                val = val.strip()
                quoted: bool = False
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                    quoted = True
                if not quoted and "#" in val:
                    val = val.split("#", 1)[0].rstrip()
                result[key] = val
        if not result:
            raise RuntimeError(
                f"{path}: `# Secrets` section missing or empty. "
                "REQ-T-057 requires every key under exactly one of "
                "`# Secrets` or `# SecretSafe`, and the leak-check "
                "needle set cannot be empty."
            )
        return result

    @staticmethod
    def _read_env_file(path: Path) -> dict[str, str]:
        """Parse a single-line `key=value` `.env`.

        Strips surrounding single or double quotes. Strips inline `#`
        comments outside quotes. Skips blank lines and full-line
        comments. Never invokes a shell, never expands `$VAR` style
        references. Failure to open raises.
        """
        result: dict[str, str] = {}
        with path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.rstrip("\r\n")
                stripped = line.lstrip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" not in stripped:
                    continue
                key, _, val = stripped.partition("=")
                key = key.strip()
                if not key:
                    continue
                val = val.strip()
                quoted: bool = False
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                    quoted = True
                if not quoted and "#" in val:
                    val = val.split("#", 1)[0].rstrip()
                result[key] = val
        return result

    @staticmethod
    def _read_gha_secret_store(env: str) -> dict[str, str]:
        raise NotImplementedError(
            "authored separately; only local .env is wired today"
        )

    @staticmethod
    def _read_hf_space_secrets(env: str) -> dict[str, str]:
        raise NotImplementedError(
            "authored separately; only local .env is wired today"
        )

    @staticmethod
    def _read_cloudflare_env_vars(env: str) -> dict[str, str]:
        raise NotImplementedError(
            "authored separately; only local .env is wired today"
        )

    @staticmethod
    def _read_azure_function_app_settings(env: str) -> dict[str, str]:
        raise NotImplementedError(
            "authored separately; only local .env is wired today"
        )

    @staticmethod
    def _read_azure_automation_variables(env: str) -> dict[str, str]:
        raise NotImplementedError(
            "authored separately; only local .env is wired today"
        )
