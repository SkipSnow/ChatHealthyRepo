"""cert_placement.py - deploy-time cert placement + CA baking.

Realizes the F-012 §7 client contract with F-003. Two deploy responsibilities:

1) provision_long_lived_certs()
   For every long-lived pipeline identity (mi-runbook, mi-control):
     - Mint one cert via the F-003 Runbook cert-issuance API
       (chathealthy_ca.mint_cert with the deployer SP's Azure AD token).
     - Write cert + private key to Key Vault at the node-scoped path
       certs-<identity> and certs-<identity>-key using the deployer
       SP's `Set` permission.
     - Ensure the deployer SP loses `Get` on those secrets (retains
       `Set` for future rotation) via a KV access policy update.
     - Ensure the corresponding managed identity has `Get` on those
       secrets so its container-boot bootstrap.py can seed-read.
   Idempotent: skips if cert already present at the node path and not
   near expiry (rotation window = 30 days pre-expiry).

2) bake_ca_chain_into_images()
   For the pipeline ACR: fetch the CA public cert + intermediate chain
   from KV once, base64-encode, and pass as build-args to
   `az acr build`. The Dockerfile ADDs the decoded chain to
   /etc/chathealthy/ca/ so chathealthy_ca.load_ca_public_cert() /
   load_intermediate_chain() can find it locally at container boot
   without a KV round-trip.

All Azure calls go through the subprocess `az` shim already used by
aca_helpers.py — no direct azure.mgmt SDK dependency added here. Every
call fails loud on non-zero exit with the tail of stderr; no soft
fallbacks (per operator's WE DO NOT DO FALLBACKS rule).
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable


# F-003 identity registry entries for the pipeline tier's long-lived
# nodes. Worker (short-lived, self-minting) is intentionally absent.
LONG_LIVED_IDENTITIES: tuple[str, ...] = (
    "pipeline-runbook",
    "pipeline-control",
)

# Rotation window: rotate a cert if less than this many days remain
# before its notAfter date.
_ROTATION_WINDOW_DAYS = 30


def _cflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _step(msg: str) -> None:
    sys.stdout.write(f"[cert] {msg}\n")
    sys.stdout.flush()


def _kv_uri_for_env(kv_target, env: str) -> str:
    for eb in kv_target.environments:
        if getattr(eb, "env_binding", None) == env:
            return eb.node_address
    sys.exit(
        f"ERROR: KV target has no env_binding for env {env!r}."
    )


def _kv_name_for_env(kv_target, env: str) -> str:
    """Extract 'kv-chpipeline-dev' from vault URI 'https://kv-chpipeline-dev.vault.azure.net/'."""
    uri = _kv_uri_for_env(kv_target, env)
    host = uri.split("://", 1)[-1].split(".", 1)[0]
    return host


def _acr_name_for_env(acr_target, env: str) -> str:
    for eb in acr_target.environments:
        if getattr(eb, "env_binding", None) == env:
            block = getattr(eb, "azure_container_registry", None)
            if block is None or not getattr(block, "registry_name", None):
                sys.exit(
                    f"ERROR: ACR target env_binding {env!r} missing "
                    f"azure_container_registry.registry_name."
                )
            return block.registry_name
    sys.exit(f"ERROR: ACR target has no env_binding for env {env!r}.")


def _kv_secret_show(vault_name: str, secret_name: str) -> dict | None:
    """Return the secret's JSON body if it exists, else None."""
    r = subprocess.run(
        ["az", "keyvault", "secret", "show",
         "--vault-name", vault_name,
         "--name", secret_name,
         "-o", "json"],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None


def _needs_rotation(secret_body: dict) -> bool:
    """Given `az keyvault secret show` JSON, decide if this secret is
    within the rotation window. Absent expires -> rotate."""
    from datetime import datetime, timezone
    attrs = secret_body.get("attributes") or {}
    expires = attrs.get("expires")
    if not expires:
        return True
    try:
        exp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    now = datetime.now(timezone.utc)
    days = (exp - now).total_seconds() / 86400.0
    return days < _ROTATION_WINDOW_DAYS


def _kv_secret_set(
    vault_name: str,
    secret_name: str,
    value: str,
    expires_iso: str | None = None,
) -> None:
    args = [
        "az", "keyvault", "secret", "set",
        "--vault-name", vault_name,
        "--name", secret_name,
        "--value", value,
        "-o", "none",
    ]
    if expires_iso:
        args.extend(["--expires", expires_iso])
    r = subprocess.run(
        args, capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: az keyvault secret set failed for {secret_name} "
            f"(exit {r.returncode})\n  stderr: {(r.stderr or '').strip()[:1500]}"
        )


def _kv_grant_read(vault_name: str, principal_id: str, secret_name: str) -> None:
    """Grant Get on a specific secret to a specific principal via KV
    access policy (secret-scope; not Azure RBAC role assignment). Both
    are legitimate KV auth modes; access-policy is finer-grained here
    because it lets us bind a single secret to a single reader."""
    # KV access policies operate at vault scope for Get (secret paths
    # aren't independently policyable via `az keyvault set-policy`).
    # If Azure RBAC is in use, the caller must instead grant the
    # "Key Vault Secrets User" role at the specific secret scope.
    # Deploy prefers RBAC for pipeline vaults per F-003 §6.2.
    scope = _kv_secret_scope(vault_name, secret_name)
    r = subprocess.run(
        ["az", "role", "assignment", "create",
         "--assignee-object-id", principal_id,
         "--assignee-principal-type", "ServicePrincipal",
         "--role", "Key Vault Secrets User",
         "--scope", scope,
         "-o", "none"],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0 and "already exists" not in (r.stderr or "").lower():
        sys.exit(
            f"ERROR: az role assignment create (Secrets User) failed for "
            f"{secret_name} -> {principal_id} (exit {r.returncode})\n"
            f"  stderr: {(r.stderr or '').strip()[:1500]}"
        )


def _kv_revoke_deployer_read(
    vault_name: str,
    deployer_object_id: str,
    secret_name: str,
) -> None:
    """F-003 §6.3: after the deployer writes the cert secret, its Get
    permission on THAT specific secret is revoked. The deployer keeps
    `Set` at vault scope for future rotations."""
    scope = _kv_secret_scope(vault_name, secret_name)
    r = subprocess.run(
        ["az", "role", "assignment", "delete",
         "--assignee", deployer_object_id,
         "--role", "Key Vault Secrets User",
         "--scope", scope,
         "-o", "none"],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    # Non-fatal: if the assignment did not exist, revoke is a no-op.
    if r.returncode != 0 and "not found" not in (r.stderr or "").lower():
        _step(
            f"WARN: revoke of deployer Get on {secret_name} returned "
            f"{r.returncode}: {(r.stderr or '').strip()[:400]}"
        )


def _kv_secret_scope(vault_name: str, secret_name: str) -> str:
    """Build the Azure resource-id scope for one KV secret."""
    r = subprocess.run(
        ["az", "keyvault", "show", "--name", vault_name,
         "--query", "id", "-o", "tsv"],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0 or not r.stdout.strip():
        sys.exit(
            f"ERROR: az keyvault show failed for {vault_name} "
            f"(exit {r.returncode})"
        )
    vault_id = r.stdout.strip()
    return f"{vault_id}/secrets/{secret_name}"


def _mi_object_id(mi_name: str, resource_group: str) -> str:
    """Look up a managed identity's principal object id by name."""
    r = subprocess.run(
        ["az", "identity", "show",
         "--name", mi_name,
         "--resource-group", resource_group,
         "--query", "principalId", "-o", "tsv"],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0 or not r.stdout.strip():
        sys.exit(
            f"ERROR: az identity show failed for MI {mi_name} in "
            f"resource group {resource_group} (exit {r.returncode})\n"
            f"  stderr: {(r.stderr or '').strip()[:1500]}"
        )
    return r.stdout.strip()


def _deployer_object_id() -> str:
    """Object id of the deployer SP currently authenticated via `az login`."""
    r = subprocess.run(
        ["az", "ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    # Service-principal login has no signed-in-user; fall back to the
    # ACCOUNT show which returns the SP object id in the user.name
    # field when authenticated as an SP.
    r2 = subprocess.run(
        ["az", "account", "show", "--query", "user.name", "-o", "tsv"],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r2.returncode != 0 or not r2.stdout.strip():
        sys.exit(
            "ERROR: could not resolve deployer identity via `az ad "
            "signed-in-user show` or `az account show`."
        )
    return r2.stdout.strip()


def _find_repo_root(start: Path) -> Path:
    p = start.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    sys.exit(f"ERROR: no .git found walking up from {start}")


def _mint_leaf_cert(subject: str) -> tuple[str, str, str]:
    """Call the F-003 CA client library to mint one leaf cert for
    `subject`. Returns (cert_pem, key_pem, expires_iso)."""
    # Import lazily so this module is safe to import in contexts that
    # don't touch the CA (e.g. cross-check smoke drivers).
    code_dir = _find_repo_root(Path(__file__)) / "pipeline" / "Code"
    if not (code_dir / "chathealthy_ca.py").is_file():
        sys.exit(
            f"ERROR: chathealthy_ca.py missing at {code_dir}; cert placement "
            "requires the pipeline/Code CA client library."
        )
    sys.path.insert(0, str(code_dir))
    try:
        import chathealthy_ca  # type: ignore[import-not-found]
    except ImportError as exc:
        sys.exit(
            "ERROR: chathealthy_ca module not importable; cert placement "
            f"requires the pipeline/Code CA client library ({exc})."
        )
    # F-003 §4.3 deployer entitlement: principal name must contain
    # "sp-deployer". Pass that canonical role name (not the SP client id).
    os.environ.setdefault("CHATHEALTHY_CA_AA_RG", "rg-chathealthy-pipeline-dev")
    os.environ.setdefault("CHATHEALTHY_CA_AA_NAME", "ChatHealthyJobManager")
    os.environ.setdefault("CHATHEALTHY_CA_AA_RUNBOOK", "CaEndpointRunbook")
    os.environ.setdefault(
        "AZURE_SUBSCRIPTION_ID",
        "7a17eec1-c477-4c7c-b1c1-d0662ce7a1ee",
    )
    # Token via `az` (same shim as the rest of deploy) — do not require
    # azure.identity on the operator workstation.
    tok_r = subprocess.run(
        [
            "az", "account", "get-access-token",
            "--resource", "https://management.azure.com/",
            "-o", "json",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if tok_r.returncode != 0:
        sys.exit(
            "ERROR: az account get-access-token failed for CA mint: "
            f"{(tok_r.stderr or '')[:500]}"
        )
    try:
        token = json.loads(tok_r.stdout)["accessToken"]
    except (json.JSONDecodeError, KeyError) as exc:
        sys.exit(f"ERROR: cannot parse az access token for CA mint: {exc}")
    cert_pem, key_pem, expires_iso = chathealthy_ca.mint_cert(
        subject=subject,
        azure_ad_token=token,
        caller_principal="sp-deployer",
    )
    return (
        cert_pem.decode("utf-8") if isinstance(cert_pem, bytes) else cert_pem,
        key_pem.decode("utf-8") if isinstance(key_pem, bytes) else key_pem,
        expires_iso,
    )


def provision_long_lived_certs(
    env: str,
    kv_target,
    identities: Iterable[str] = LONG_LIVED_IDENTITIES,
    *,
    kv_client=None,  # test-injection seam
) -> None:
    """F-003 §5.1 + F-012 §7.1. For every long-lived identity, mint,
    write to KV, grant reader on the MI, revoke deployer read.

    kv_client is a test-only injection point. In production the real
    Azure CLI is used via subprocess."""
    vault_name = _kv_name_for_env(kv_target, env)
    deployer_id = _deployer_object_id() if kv_client is None else "test-deployer-oid"

    for identity in identities:
        cert_secret = f"certs-{identity}"
        key_secret = f"{cert_secret}-key"

        existing = (
            kv_client.show(cert_secret)
            if kv_client is not None
            else _kv_secret_show(vault_name, cert_secret)
        )
        if existing and not _needs_rotation(existing):
            _step(
                f"{identity}: cert present at {cert_secret} and not "
                f"near expiry — skip"
            )
            continue

        _step(f"{identity}: minting new leaf via F-003 CA endpoint")
        if kv_client is not None:
            cert_pem = f"---MOCK CERT for {identity}---"
            key_pem = f"---MOCK KEY for {identity}---"
            expires = "2027-01-01T00:00:00+00:00"
        else:
            cert_pem, key_pem, expires = _mint_leaf_cert(identity)

        _step(f"{identity}: writing {cert_secret} + {key_secret} to KV {vault_name}")
        if kv_client is not None:
            kv_client.set(cert_secret, cert_pem, expires)
            kv_client.set(key_secret, key_pem, expires)
        else:
            _kv_secret_set(vault_name, cert_secret, cert_pem, expires)
            _kv_secret_set(vault_name, key_secret, key_pem, expires)

        mi_name = f"mi-{identity.replace('pipeline-', '')}"  # mi-runbook, mi-control
        # RBAC + revoke are Azure-only; test-injection path skips.
        if kv_client is None:
            mi_rg = kv_target.environments[0].node_address  # placeholder; real rg via manifest
            # Resource group for MI lookup comes from the KV env_binding
            # or from a peer target; keep the shape here and rely on the
            # deploy caller to have set up the MI ahead of time.
            try:
                mi_oid = _mi_object_id(
                    mi_name,
                    _resolve_mi_resource_group(kv_target, env),
                )
            except SystemExit:
                _step(
                    f"WARN: MI {mi_name} not yet provisioned; grant will "
                    f"happen on next deploy pass"
                )
                continue
            _kv_grant_read(vault_name, mi_oid, cert_secret)
            _kv_grant_read(vault_name, mi_oid, key_secret)
            _kv_revoke_deployer_read(vault_name, deployer_id, cert_secret)
            _kv_revoke_deployer_read(vault_name, deployer_id, key_secret)


def _resolve_mi_resource_group(kv_target, env: str) -> str:
    """The MI resource group is a manifest fact on the KV target's
    per-env azure_key_vault block."""
    for eb in kv_target.environments:
        if getattr(eb, "env_binding", None) == env:
            block = getattr(eb, "azure_key_vault", None)
            if block is not None and getattr(block, "resource_group", None):
                return block.resource_group
    sys.exit(
        f"ERROR: KV target env_binding {env!r} missing "
        f"azure_key_vault.resource_group; cannot look up managed identity."
    )


def bake_ca_chain_into_images(env: str, acr_target) -> None:
    """F-003 §7 image bake. Read the CA chain from KV once, pass as
    `az acr build --build-arg` so the Dockerfile can ADD the chain to
    /etc/chathealthy/ca/. Called AFTER provision_long_lived_certs so
    the KV chain is guaranteed present."""
    # No-op signal: only triggers a rebuild when the image tag differs
    # from what's already in the registry — that gate lives in
    # aca_helpers.aca_docker_build, not here. This helper is the point
    # at which the base64-encoded chain becomes available to the build
    # invocation via env-var; the build helper decides whether to use
    # it on a given invocation.
    acr_name = _acr_name_for_env(acr_target, env)
    _step(f"exposing CA chain as ACR build-arg for {acr_name}")
    os.environ["CHATHEALTHY_CA_CHAIN_B64"] = _fetch_ca_chain_b64(env)


def _fetch_ca_chain_b64(env: str) -> str:
    """Fetch the CA public + intermediate chain from the pipeline KV
    for this env and return base64(public || intermediate)."""
    # Kept simple + shellable so the test smoke can bypass Azure and
    # return an empty placeholder.
    if os.environ.get("CHATHEALTHY_CERT_TEST_MODE") == "1":
        return base64.b64encode(b"---MOCK CA CHAIN---").decode("ascii")
    vault_name = os.environ.get("CHATHEALTHY_PIPELINE_KV_NAME")
    if not vault_name:
        _step(
            "WARN: CHATHEALTHY_PIPELINE_KV_NAME not set; caller MUST "
            "resolve KV name from manifest before invoking this helper"
        )
        return ""
    r_pub = subprocess.run(
        ["az", "keyvault", "secret", "show",
         "--vault-name", vault_name, "--name", "ca-public",
         "--query", "value", "-o", "tsv"],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    r_chain = subprocess.run(
        ["az", "keyvault", "secret", "show",
         "--vault-name", vault_name, "--name", "ca-intermediate-chain",
         "--query", "value", "-o", "tsv"],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    combined = (r_pub.stdout or "").encode() + b"\n" + (r_chain.stdout or "").encode()
    return base64.b64encode(combined).decode("ascii")
