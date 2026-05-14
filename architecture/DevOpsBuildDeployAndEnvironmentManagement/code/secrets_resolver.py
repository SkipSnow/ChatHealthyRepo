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


_STORE_LOCAL_ENV: str = "local_env"
_STORE_GHA: str = "gha_secret"
_STORE_HF_SPACE: str = "hf_space_secret"
_STORE_CLOUDFLARE: str = "cloudflare_env"


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
        raise RuntimeError(
            f"binding {key!r} maps to unknown store {store!r}; no fallback"
        )

    def env_values_for_leak_check(self, env_file: Path) -> set[str]:
        """Return the set of values present in a `.env`.

        Parses keys + values from each `key=value` line and returns the
        values only — keys are public labels (variable names), values
        are the secret material that the leak-check must guard against.
        Nothing from this set is ever logged or persisted by this method.
        """
        parsed = self._read_env_file(env_file)
        return {v for v in parsed.values() if v}

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
