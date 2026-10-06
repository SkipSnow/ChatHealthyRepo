"""Expose the CA chain to an image build.

Reads the CA root and intermediate certificates from Key Vault and sets
them as base64 environment variables. The deploy passes those to
`az acr build --build-arg`; Dockerfile.control and Dockerfile.worker
decode them to /etc/chathealthy/ca/root.pem and intermediate.pem and
refuse the build unless both decode to a certificate. A container then
finds the chain on its own filesystem at boot with no Key Vault call.

Azure calls go through the subprocess `az` shim. Every one fails loud on
a non-zero exit with the tail of stderr; no fallbacks.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402


def _cflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _step(msg: str) -> None:
    sys.stdout.write(f"[cert] {msg}\n")
    sys.stdout.flush()


def _kv_uri_for_env(kv_target, env: str) -> str:
    for eb in kv_target.environments:
        if getattr(eb, "env_binding", None) == env:
            return eb.node_address
    raise ChatHealthyException(
        mode="aborted",
        component="cert_placement",
        message=f"ERROR: KV target has no env_binding for env {env!r}.")


def _kv_name_for_env(kv_target, env: str) -> str:
    """Extract 'kv-chpipeline-dev' from vault URI 'https://kv-chpipeline-dev.vault.azure.net/'."""
    uri = _kv_uri_for_env(kv_target, env)
    host = uri.split("://", 1)[-1].split(".", 1)[0]
    return host


def _block_field(block, key: str):
    """Read a field from an env block that may be a dict or an object."""
    if block is None:
        return None
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


def _acr_name_for_env(acr_target, env: str) -> str:
    for eb in acr_target.environments:
        if getattr(eb, "env_binding", None) == env:
            block = getattr(eb, "azure_container_registry", None)
            name = _block_field(block, "registry_name")
            if not name:
                raise ChatHealthyException(
                    mode="aborted",
                    component="cert_placement",
                    message=f"ERROR: ACR target env_binding {env!r} missing "
                    f"azure_container_registry.registry_name.")
            return name
    raise ChatHealthyException(
        mode="aborted",
        component="cert_placement",
        message=f"ERROR: ACR target has no env_binding for env {env!r}.")


def bake_ca_chain_into_images(env: str, acr_target, kv_target) -> None:
    """Set CHATHEALTHY_CA_ROOT_B64 and CHATHEALTHY_CA_INTERMEDIATE_B64 in
    this process, read from the vault bound to this env."""
    acr_name = _acr_name_for_env(acr_target, env)
    vault_name = _kv_name_for_env(kv_target, env)
    _step(f"exposing CA chain as ACR build-arg for {acr_name}")
    root_b64, intermediate_b64 = _fetch_ca_pem_b64s(vault_name)
    os.environ["CHATHEALTHY_CA_ROOT_B64"] = root_b64
    os.environ["CHATHEALTHY_CA_INTERMEDIATE_B64"] = intermediate_b64


def _fetch_ca_pem_b64s(vault_name: str) -> tuple[str, str]:
    """Fetch ca-root-cert + ca-intermediate-cert; return (root_b64, int_b64)."""
    if os.environ.get("CHATHEALTHY_CERT_TEST_MODE") == "1":
        return (
            base64.b64encode(b"---MOCK ROOT---").decode("ascii"),
            base64.b64encode(b"---MOCK INTERMEDIATE---").decode("ascii"),
        )
    root = _kv_secret_value(vault_name, "ca-root-cert")
    intermediate = _kv_secret_value(vault_name, "ca-intermediate-cert")
    if "BEGIN CERTIFICATE" not in root or "END CERTIFICATE" not in root:
        raise ChatHealthyException(
            mode="aborted",
            component="cert_placement",
            message=f"ERROR: KV secret ca-root-cert in {vault_name} is not a PEM cert "
            f"(len={len(root)}).")
    if (
        "BEGIN CERTIFICATE" not in intermediate
        or "END CERTIFICATE" not in intermediate
    ):
        raise ChatHealthyException(
            mode="aborted",
            component="cert_placement",
            message=f"ERROR: KV secret ca-intermediate-cert in {vault_name} is not a "
            f"PEM cert (len={len(intermediate)}).")
    return (
        base64.b64encode(root.encode("utf-8")).decode("ascii"),
        base64.b64encode(intermediate.encode("utf-8")).decode("ascii"),
    )


def _kv_secret_value(vault_name: str, secret_name: str) -> str:
    r = subprocess.run(
        [
            "az", "keyvault", "secret", "show",
            "--vault-name", vault_name,
            "--name", secret_name,
            "--query", "value",
            "-o", "tsv",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        raise ChatHealthyException(
            mode="aborted",
            component="cert_placement",
            message=f"ERROR: cannot read KV secret {secret_name!r} from "
            f"{vault_name}: {(r.stderr or '').strip()[:800]}")
    return r.stdout.strip()


def mint_declared_identity_certs_if_missing(coll, env: str) -> None:
    """Mint every IdentityCatalog certificate that is declared but absent from
    its vault. The certificate is self-signed and its Subject DN is the
    declaration's, so the certificate IS the identity's credential -- its CN
    equals identity_id, which the connection utility verifies at auth time.

    Show-then-create, idempotent: a certificate already present under the
    declared name is left untouched. The deploy mints a MISSING credential; it
    does not rotate, overwrite, or grant. The database user and its role stay
    manual entitlement work."""
    for ident in (coll.identity_catalog or []):
        cert = ident.get("certificate")
        if not cert:
            continue
        kv_target = coll.by_target_id(cert["vault_target_ref"])
        if kv_target is None:
            raise ChatHealthyException(
                mode="aborted",
                component="cert_placement",
                message=f"ERROR: identity {ident.get('identity_id')!r} names "
                f"vault_target_ref {cert['vault_target_ref']!r}, which is not a target.")
        vault_name = _kv_name_for_env(kv_target, env)
        name = cert["vault_secret_name"]
        subject = cert["subject_dn"]
        if _kv_certificate_exists(vault_name, name):
            _step(f"certificate {name!r} present in {vault_name}; leaving it")
            continue
        _step(f"minting self-signed certificate {name!r} subject={subject!r} in {vault_name}")
        _mint_self_signed_certificate(vault_name, name, subject)


def _kv_certificate_exists(vault_name: str, name: str) -> bool:
    r = subprocess.run(
        [
            "az", "keyvault", "certificate", "show",
            "--vault-name", vault_name,
            "--name", name,
        ],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    return r.returncode == 0


def _mint_self_signed_certificate(vault_name: str, name: str, subject: str) -> None:
    policy = {
        "issuerParameters": {"name": "Self"},
        "keyProperties": {
            "exportable": True,
            "keyType": "RSA",
            "keySize": 2048,
            "reuseKey": False,
        },
        "secretProperties": {"contentType": "application/x-pkcs12"},
        "x509CertificateProperties": {
            "subject": subject,
            "validityInMonths": 12,
            "keyUsage": ["digitalSignature", "keyEncipherment"],
        },
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(policy, fh)
        r = subprocess.run(
            [
                "az", "keyvault", "certificate", "create",
                "--vault-name", vault_name,
                "--name", name,
                "--policy", f"@{path}",
            ],
            capture_output=True, text=True,
            creationflags=_cflags(), shell=(sys.platform == "win32"),
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if r.returncode != 0:
        raise ChatHealthyException(
            mode="aborted",
            component="cert_placement",
            message=f"ERROR: minting certificate {name!r} in {vault_name} failed: "
            f"{(r.stderr or '').strip()[:800]}")


