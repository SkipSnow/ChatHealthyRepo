"""Secret value resolver and leak-check utility.

A name is bound to one store and that store is consulted. The one
exception is the local `.env`, which is a single workstation's copy of
credentials the environment's vault also holds: a name bound to it that
the file does not carry is read from that environment's vault instead.
This is not a per-target accommodation -- it holds for every target and
every secret bound to `local_env`, because a deployment that can only be
run from the workstation that happens to hold a credential is not a
deployment. A name neither store holds raises, naming the vault and the
secret it looked for.

The leak-check helper exposes the set of values present in a local
`.env` so the Crosswalk can guarantee no `.env` value bytes ever leak
into the brain artifact's embedded_content (or any other string field).
The helper NEVER logs or writes the values it reads.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

if TYPE_CHECKING:
    from target_record import DeploymentCollection


_STORE_LOCAL_ENV: str = "local_env"
_STORE_HF_SPACE: str = "hf_space_secret"
_STORE_CLOUDFLARE: str = "cloudflare_env"
_STORE_AZURE_FA: str = "azure_function_app_setting"
_STORE_AZURE_AA: str = "azure_automation_variable"
_STORE_AZURE_AA_WEBHOOK: str = "azure_automation_webhook"
_STORE_AZURE_KEY_VAULT: str = "azure_key_vault"

# A vault secret is named for what it is and for nothing else. It does not
# carry the environment, the target or the package that consumes it: those
# are deployment facts, and a vault that knows them has to be rewritten
# every time a deployment changes. It also makes rotation unmanageable --
# one key would exist under as many names as there are environments, and
# rotating it would mean finding every copy.
#
# Isolation between environments is therefore the vault, not the name. Each
# environment reads the vault its own record declares, and inside that vault
# every secret is named plainly. Rotating a key is one write per vault.
#
# Key Vault secret names admit letters, digits and hyphens only, so the
# underscore in an environment-variable name becomes a hyphen. Case is
# preserved, which is what makes the names already in the vault --
# ANTHROPIC-API-KEY, OPENAI-API-KEY -- the same names this produces rather
# than a second set beside them.
def vault_secret_name(env_var_name: str) -> str:
    return env_var_name.replace("_", "-")


# Every qualifier that names a store the resolver reads from, as opposed to a
# source qualifier the deploy computes (peer_url:, local_cert_file:, env_name,
# rename_from:, literal:). Callers that dispatch on qualifiers test membership
# here rather than naming one store: the HF handler tested `== "local_env"`
# and so refused every target the moment its secrets moved to the vault.
STORE_IDS: frozenset[str] = frozenset({
    _STORE_LOCAL_ENV, _STORE_HF_SPACE, _STORE_CLOUDFLARE, _STORE_AZURE_FA,
    _STORE_AZURE_AA, _STORE_AZURE_AA_WEBHOOK, _STORE_AZURE_KEY_VAULT,
})



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


class SecretsResolver:
    """Resolve a bound secret to a value via the store it is bound to.

    Construction takes a mapping `(secret_name, env) -> store_id` so the
    binding is explicit. `resolve` looks up the bound store and reads the
    value from it. A name bound to `local_env` that the file does not
    carry is read from the environment's vault -- one rule, applied
    wherever that binding appears, not a per-target accommodation.
    """

    def __init__(
        self,
        bindings: dict[tuple[str, str], str] | None = None,
        env_file: Path | None = None,
        vaults: dict[str, str] | None = None,
    ) -> None:
        self._bindings: dict[tuple[str, str], str] = dict(bindings or {})
        self._env_file: Path | None = env_file
        # env_binding -> vault name, read from the manifest. One vault per
        # environment is what keeps dev's credentials unreadable by prod.
        self._vaults: dict[str, str] = dict(vaults or {})
        self._env_cache: dict[str, str] | None = None
        self._vault_cache: dict[tuple[str, str], str] = {}
        self._seeded: list[tuple[str, str]] = []

    def seeded(self) -> list[tuple[str, str]]:
        """(vault, secret name) pairs this resolver wrote because they were absent.

        Names only. The caller reports what it had to create so an absent
        secret is visible rather than silently manufactured.
        """
        return list(self._seeded)

    @classmethod
    def from_collection(
        cls,
        coll: "DeploymentCollection",
        env_file: Path | None = None,
    ) -> "SecretsResolver":
        """Build bindings by reading per-target `secrets` declarations.

        Per EPIC-008-F-012: bindings dict MUST be
        constructed by reading per-target key declarations from the
        manifest. Each TargetRecord enumerates its own keys; each
        (name, env_binding) pair in any target maps to that target's
        bound store_id. If two targets declare the same (name, env)
        with different store ids, that is a hairball — raise.
        """
        bindings: dict[tuple[str, str], str] = {}
        _STORE_IDS = {
            _STORE_LOCAL_ENV, _STORE_HF_SPACE, _STORE_CLOUDFLARE,
            _STORE_AZURE_FA, _STORE_AZURE_AA, _STORE_AZURE_AA_WEBHOOK,
            _STORE_AZURE_KEY_VAULT,
        }
        vaults = cls._vaults_from_collection(coll)
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
                            raise ChatHealthyException(
            mode="value_error",
            component="secrets_resolver",
            message=f"binding conflict for {key!r}: "
                                f"target {record.target_id!r} declares "
                                f"{store_id!r}, prior target declared {existing!r}")
                        bindings[key] = store_id
        return cls(bindings=bindings, env_file=env_file, vaults=vaults)

    @staticmethod
    def _vaults_from_collection(coll: "DeploymentCollection") -> dict[str, str]:
        """env_binding -> vault name, taken from the record and nowhere else.

        An environment with no vault declared gets no entry, and resolving a
        vault-bound secret for it fails. That is deliberate: the alternative
        is a default, and a default here means one environment quietly
        reading another environment's credentials.
        """
        found: dict[str, str] = {}
        for record in coll:
            for binding in record.environments:
                block = binding.azure_key_vault
                if not block:
                    continue
                name = block.get("vault_name")
                if not name:
                    continue
                prior = found.get(binding.env_binding)
                if prior is not None and prior != name:
                    raise ChatHealthyException(
                        mode="value_error",
                        component="secrets_resolver",
                        message=f"env {binding.env_binding!r} declares two vaults "
                                f"({prior!r} and {name!r}); an environment has one "
                                f"vault or its secrets have no single home")
                found[binding.env_binding] = name
        return found

    def vault_for(self, env: str) -> str:
        vault = self._vaults.get(env)
        if not vault:
            raise ChatHealthyException(
                mode="manifest_incomplete",
                component="secrets_resolver",
                message=f"env {env!r} declares no Key Vault in "
                        f"deployment_architecture.json, so its secrets have "
                        f"nowhere to be read from. Declare an azure_key_vault "
                        f"binding for {env!r}; this does not fall back to "
                        f"another environment's vault.")
        return vault

    def _resolve_from_vault(self, name: str, env: str) -> str:
        """Read one named secret from the environment's own vault.

        Seeds the vault from the local .env when, and only when, the secret
        is genuinely absent. A read that fails for any other reason -- no
        access, vault unreachable, az not signed in -- is NOT absence, and
        treating it as absence would overwrite a live credential with
        whatever this workstation happens to hold.
        """
        vault = self.vault_for(env)
        key = (vault, vault_secret_name(name))
        if key in self._vault_cache:
            return self._vault_cache[key]
        value, absent = self._vault_read(vault, key[1])
        if absent:
            value = self._seed_vault(vault, key[1], name, env)
        self._vault_cache[key] = value
        return value

    def _has_local_value(self, name: str) -> bool:
        """A name in the .env with something after the '=' .

        A bare `NAME=` satisfies every check that asks whether the name is
        there, and satisfies nothing that asks for a credential. It would
        deploy as an empty environment variable and the service would fail
        authenticating against a peer, at runtime, with a value that looks
        configured.
        """
        if self._env_file is None or not Path(self._env_file).is_file():
            return False
        if self._env_cache is None:
            self._env_cache = self._read_env_file(Path(self._env_file))
        return bool((self._env_cache.get(name) or "").strip())

    def _seed_vault(self, vault: str, secret_name: str,
                    name: str, env: str) -> str:
        if self._env_file is None or not Path(self._env_file).is_file():
            raise ChatHealthyException(
                mode="key_error",
                component="secrets_resolver",
                message=f"{vault} holds no secret {secret_name!r} and there is "
                        f"no .env to seed it from")
        if not self._has_local_value(name):
            raise ChatHealthyException(
                mode="key_error",
                component="secrets_resolver",
                message=f"{name!r} is declared for env {env!r} but no value "
                        f"exists for it: {vault} holds no {secret_name!r} and "
                        f"{self._env_file} carries no value. Nothing can supply "
                        f"it, and an empty secret is not written to the vault.")
        self._vault_write(vault, secret_name, self._env_cache[name])
        self._seeded.append((vault, secret_name))
        return self._env_cache[name]

    @staticmethod
    def _vault_read(vault: str, secret_name: str) -> tuple[str, bool]:
        """Return (value, absent). Never returns a value and absent together.

        az reports a missing secret and a refused one the same way -- a
        non-zero exit -- so the message decides. Anything that is not
        recognisably 'this secret does not exist' raises, because a resolver
        that cannot tell those apart will seed over a secret it simply was
        not allowed to read.
        """
        import subprocess  # noqa: PLC0415
        completed = subprocess.run(
            ["az", "keyvault", "secret", "show",
             "--vault-name", vault, "--name", secret_name,
             "--query", "value", "-o", "tsv"],
            capture_output=True, text=True, shell=(_ch_sys.platform == "win32"))
        if completed.returncode == 0:
            return (completed.stdout or "").strip(), False
        detail = ((completed.stderr or "") + (completed.stdout or "")).strip()
        if "SecretNotFound" in detail or "was not found in this key vault" in detail:
            return "", True
        raise ChatHealthyException(
            mode="vault_unreachable",
            component="secrets_resolver",
            message=f"{vault}/{secret_name} could not be read, and this is not "
                    f"the vault reporting it absent: {detail[:600]}")

    @staticmethod
    def _vault_write(vault: str, secret_name: str, value: str) -> None:
        import subprocess  # noqa: PLC0415
        completed = subprocess.run(
            ["az", "keyvault", "secret", "set",
             "--vault-name", vault, "--name", secret_name,
             "--value", value, "-o", "none"],
            capture_output=True, text=True, shell=(_ch_sys.platform == "win32"))
        if completed.returncode != 0:
            raise ChatHealthyException(
                mode="vault_unreachable",
                component="secrets_resolver",
                message=f"{vault}/{secret_name} could not be written: "
                        f"{((completed.stderr or '') + (completed.stdout or '')).strip()[:600]}")

    def verify_matches_local(self, name: str, env: str) -> tuple[bool, str]:
        """Confirm the vault's value and the .env's value are the same bytes.

        The .env is a fair copy kept by hand, so nothing has ever guaranteed
        it agrees with the vault. Two copies of a credential that are
        expected to be identical and are never compared will differ, and the
        first anyone learns of it is a service authenticating with a value
        its peer retired.

        Neither copy wins on mismatch. Preferring the vault would let a stale
        vault silently outrank a corrected .env; preferring the .env would
        push a workstation's value over a rotated one. The deploy stops and
        the operator decides which is right.

        Compared by digest, so no value is ever logged or returned.
        """
        import hashlib  # noqa: PLC0415

        store = self._effective_store(name, env)
        if store != _STORE_AZURE_KEY_VAULT:
            return True, f"{name} is not vault-bound; nothing to compare"
        vault = self.vault_for(env)
        secret_name = vault_secret_name(name)
        vault_value, absent = self._vault_read(vault, secret_name)
        if absent:
            return True, f"absent from {vault}; the deploy seeds it"
        if self._env_file is None or not Path(self._env_file).is_file():
            return True, f"{vault} holds it; no .env here to compare against"
        if self._env_cache is None:
            self._env_cache = self._read_env_file(Path(self._env_file))
        if name not in self._env_cache:
            # The vault is the source of truth and it has the value. The fair
            # copy simply does not carry it, which is a gap in the copy and
            # not a reason to refuse a deployment the vault can supply.
            return True, f"{vault} holds it; {self._env_file} does not carry it"
        digest = hashlib.sha256(vault_value.encode("utf-8")).hexdigest()[:12]
        local = hashlib.sha256(
            self._env_cache[name].encode("utf-8")).hexdigest()[:12]
        if digest == local:
            return True, f"{vault}/{secret_name} matches .env ({digest})"
        return False, (f"{vault}/{secret_name} and {self._env_file} hold "
                       f"different values for {name} (vault {digest}, "
                       f"local {local}). Neither is assumed correct.")

    def exists(self, name: str, env: str) -> tuple[bool, str]:
        """Can this secret be produced for this environment? Value never returned.

        This is what the build asks. A build that packaged the value would
        put a credential in a build directory and then in an image layer;
        a build that asks nothing ships something that cannot be deployed.
        Asking for presence is the only thing that is both safe and useful.
        """
        store = self._effective_store(name, env)
        if store is None:
            return False, f"no binding registered for {name!r} in env {env!r}"
        if store == _STORE_AZURE_KEY_VAULT:
            vault = self.vault_for(env)
            value, absent = self._vault_read(vault, vault_secret_name(name))
            if not absent and value:
                return True, f"{vault}/{vault_secret_name(name)}"
            if not absent:
                return False, (f"{vault} holds {vault_secret_name(name)!r} with an "
                               f"empty value, which authenticates nothing")
            if self._has_local_value(name):
                return True, (f"absent from {vault}; the deploy will seed it "
                              f"from {self._env_file}")
            return False, (f"{vault} holds no {vault_secret_name(name)!r} and "
                           f"{self._env_file} carries no value for it")
        if store == _STORE_LOCAL_ENV:
            if self._has_local_value(name):
                return True, str(self._env_file)
            # Not in the .env -- which is one workstation's copy, not the
            # authority -- so the environment's vault is asked. Same order
            # resolve() reads in, so what the build says is deployable and
            # what the deploy can actually fetch cannot disagree. Neither
            # holding it fails the deploy, which is the point: the value is
            # missing everywhere it is meant to be.
            vault = self.vault_for(env) if self._vaults.get(env) else None
            if vault:
                value, absent = self._vault_read(vault, vault_secret_name(name))
                if not absent and value:
                    return True, f"{vault}/{vault_secret_name(name)}"
            if self._env_file is None or not Path(self._env_file).is_file():
                return False, (f"{name!r} is bound to {store!r} with no .env to "
                               f"read and no value in {vault or 'any vault'}")
            if name in (self._env_cache or {}):
                return False, (f"{name!r} is present in {self._env_file} with an "
                               f"empty value; a name without a value is not a "
                               f"secret")
            return False, (f"{name!r} is in neither {self._env_file} nor "
                           f"{vault or 'any vault'} "
                           f"(as {vault_secret_name(name)!r})")
        # The remaining stores are written BY a deploy rather than read by
        # one -- the value is computed at deploy time and pushed onto the
        # target. There is nothing for a build to verify the existence of.
        return True, f"{store} (established at deploy time)"

    def _effective_store(self, name: str, env: str) -> str | None:
        """The store this name is actually read from for this environment.

        A vault-bound secret in the local environment comes from the
        workstation .env, because that is where a local deployment genuinely
        gets it: LocalDeploy hands the containers the .env wholesale and
        never constructs a resolver. There is no local vault and there should
        not be one -- standing a cloud vault behind a laptop's containers
        would put cloud credentials on the laptop.

        This is not one environment borrowing another's vault, which stays
        forbidden. Local's store is different in kind, and saying so here
        keeps every caller from having to know it.
        """
        store = self._bindings.get((name, env))
        if store == _STORE_AZURE_KEY_VAULT and env == "local":
            return _STORE_LOCAL_ENV
        return store

    def resolve(self, name: str, env: str) -> str:
        key = (name, env)
        store = self._effective_store(name, env)
        if store is None:
            raise ChatHealthyException(
            mode="key_error",
            component="secrets_resolver",
            message=f"no binding registered for secret {name!r} in env {env!r}")
        if store == _STORE_AZURE_KEY_VAULT:
            return self._resolve_from_vault(name, env)
        if store == _STORE_LOCAL_ENV:
            if self._env_file is None:
                raise ChatHealthyException(
            mode="runtime_error",
            component="secrets_resolver",
            message=f"binding {key!r} maps to {_STORE_LOCAL_ENV!r} "
                    "but no env_file was provided at construction")
            if self._env_cache is None:
                self._env_cache = self._read_env_file(self._env_file)
            if self._has_local_value(name):
                return self._env_cache[name]
            if not self._vaults.get(env):
                # The environment has no vault and is not meant to have one.
                # There is nowhere else to look, so this fails here rather
                # than reaching into another environment's vault.
                raise ChatHealthyException(
                    mode="key_error",
                    component="secrets_resolver",
                    message=f"secret {name!r} is declared for env {env!r} and "
                            f"is not in {self._env_file}. That environment "
                            f"declares no vault, so nothing else can supply it.")
            # Absent from the .env, so the environment's own vault answers.
            # This holds for every target and every secret bound to the
            # local env: the .env is one workstation's copy, and a
            # deployment that can only be run from the workstation that
            # happens to hold a credential is not a deployment. A name the
            # vault does not hold either still raises -- from the vault
            # path, naming the vault and the secret it looked for.
            return self._resolve_from_vault(name, env)
        if store == _STORE_HF_SPACE:
            self._read_hf_space_secrets(env)
        if store == _STORE_CLOUDFLARE:
            self._read_cloudflare_env_vars(env)
        if store == _STORE_AZURE_FA:
            self._read_azure_function_app_settings(env)
        if store == _STORE_AZURE_AA:
            self._read_azure_automation_variables(env)
        if store == _STORE_AZURE_AA_WEBHOOK:
            raise ChatHealthyException(
            mode="runtime_error",
            component="secrets_resolver",
            message=f"binding {key!r} maps to {_STORE_AZURE_AA_WEBHOOK!r}; "
                f"this store is NEVER resolved through SecretsResolver. "
                f"The upstream azure_automation_runbook target's deploy "
                f"step mints/reuses the webhook and pushes the URL onto "
                f"the consumer target via UPSERT. The consumer's own "
                f"deploy handler MUST skip secrets[] entries carrying "
                f"this store before calling resolve().")
        raise ChatHealthyException(
            mode="runtime_error",
            component="secrets_resolver",
            message=f"binding {key!r} maps to unknown store {store!r}; no fallback")

    def env_values_for_leak_check(self, env_file: Path) -> set[str]:
        """Return the set of secret VALUES the leak-check must guard.

        Per EPIC-008-F-012 the `.env` is organized into
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
            raise ChatHealthyException(
            mode="runtime_error",
            component="secrets_resolver",
            message=f"{path}: `# Secrets` section missing or empty. "
                "REQ-T-057 requires every key under exactly one of "
                "`# Secrets` or `# SecretSafe`, and the leak-check "
                "needle set cannot be empty.")
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
    def _read_hf_space_secrets(env: str) -> dict[str, str]:
        raise ChatHealthyException(
            mode="not_implemented",
            component="secrets_resolver",
            message="authored separately; only local .env is wired today")

    @staticmethod
    def _read_cloudflare_env_vars(env: str) -> dict[str, str]:
        raise ChatHealthyException(
            mode="not_implemented",
            component="secrets_resolver",
            message="authored separately; only local .env is wired today")

    @staticmethod
    def _read_azure_function_app_settings(env: str) -> dict[str, str]:
        raise ChatHealthyException(
            mode="not_implemented",
            component="secrets_resolver",
            message="authored separately; only local .env is wired today")

    @staticmethod
    def _read_azure_automation_variables(env: str) -> dict[str, str]:
        raise ChatHealthyException(
            mode="not_implemented",
            component="secrets_resolver",
            message="authored separately; only local .env is wired today")
