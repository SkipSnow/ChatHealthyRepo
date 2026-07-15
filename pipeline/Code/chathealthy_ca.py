"""chathealthy_ca.py - CA client library.

Callable from:
  - deploy_chathealthy.py (deploy-time long-lived cert provisioning path;
    the deployer service principal authenticates)
  - Runbook scripts that need a fresh cert
  - container bootstrap.py (Worker self-mint path; the Worker's attached
    managed identity authenticates)

Two mint paths differ only in HOW the Azure AD token is acquired:
  - mint_cert(subject, azure_ad_token) - caller passes the token in
  - mint_via_managed_identity(subject, mi_credential) - caller passes a
    credential object; we call get_token on it

Both funnel to the same ARM webhook that fires ca_endpoint_runbook.

Public helpers for chain trust:
  - load_ca_public_cert()    -> intermediate CA cert bytes (leaf's issuer)
  - load_intermediate_chain() -> concatenated intermediate + root PEM

Both public helpers can read from either:
  (a) the local filesystem when the container image was baked with the
      chain at /etc/chathealthy/ca/  (deploy-time flow, no runtime KV read)
  (b) Key Vault via managed identity, as a fallback for local dev where
      the image bake step didn't happen
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Optional, Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


LOG = logging.getLogger(__name__)


# Default CA chain locations baked into container images by the deployer.
CA_CHAIN_DIR = os.environ.get("CHATHEALTHY_CA_DIR", "/etc/chathealthy/ca")
CA_INTERMEDIATE_CERT_PATH = os.path.join(CA_CHAIN_DIR, "intermediate.pem")
CA_ROOT_CERT_PATH = os.path.join(CA_CHAIN_DIR, "root.pem")

# ARM webhook that fires ca_endpoint_runbook. The deployer stores the
# webhook URL in KV; local dev sets it via env.
CA_WEBHOOK_URL_ENV = "CHATHEALTHY_CA_WEBHOOK_URL"

# KV secret names (mirror ca_helpers constants; duplicated here so this
# module remains importable from front-end code that never touches the
# deploy dir).
_KV_INTERMEDIATE_CERT = "ca-intermediate-cert"
_KV_ROOT_CERT = "ca-root-cert"


# ===========================================================================
# CSR construction (client-side)
# ===========================================================================
def _generate_client_keypair(bits: int = 2048) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def _build_csr(subject_cn: str,
               key: rsa.RSAPrivateKey) -> x509.CertificateSigningRequest:
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ChatHealthy.ai"),
        x509.NameAttribute(NameOID.COMMON_NAME, subject_cn),
    ])
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(subject)
        .sign(key, hashes.SHA256())
    )


# ===========================================================================
# Webhook invocation
# ===========================================================================
def _post_mint_request(csr_pem: bytes,
                       subject_cn: str,
                       caller_principal: str,
                       azure_ad_token: str) -> dict:
    webhook_url = os.environ.get(CA_WEBHOOK_URL_ENV, "").strip()
    if not webhook_url:
        raise Exception(
            f"CA webhook URL not configured; set env {CA_WEBHOOK_URL_ENV}"
        )
    payload = {
        "csr_pem": csr_pem.decode("ascii"),
        "requested_subject": subject_cn,
        "caller_principal": caller_principal,
        "caller_ad_token": azure_ad_token,
    }
    req = urllib.request.Request(
        webhook_url,
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read().decode("utf-8")
    parsed = json.loads(body)
    if "error" in parsed:
        raise Exception(
            f"CA refused mint at step '{parsed.get('refused_at', '?')}': "
            f"{parsed['error']}"
        )
    if "leaf_cert_pem" not in parsed:
        raise Exception(f"CA response missing leaf_cert_pem: {body[:400]}")
    return parsed


# ===========================================================================
# Public mint API
# ===========================================================================
def mint_cert(subject: str,
              azure_ad_token: str,
              caller_principal: Optional[str] = None
              ) -> Tuple[bytes, bytes]:
    """Mint a leaf cert for `subject`. Returns (cert_pem, key_pem).

    caller_principal - the display name of the AD principal the token
    represents. If not passed, defaults to the AZURE_CLIENT_ID env var
    (typical inside a container with a user-assigned managed identity).
    """
    principal = caller_principal or os.environ.get("AZURE_CLIENT_ID", "")
    if not principal:
        raise Exception(
            "caller_principal not supplied and AZURE_CLIENT_ID env not set"
        )
    key = _generate_client_keypair()
    csr = _build_csr(subject, key)
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    response = _post_mint_request(csr_pem, subject, principal, azure_ad_token)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    LOG.info("chathealthy_ca.mint_cert: minted cert for subject=%s "
             "not_after=%s serial=%s",
             subject, response.get("not_after"), response.get("serial"))
    return response["leaf_cert_pem"].encode("ascii"), key_pem


def mint_via_managed_identity(subject: str,
                              mi_credential,
                              caller_principal: Optional[str] = None
                              ) -> Tuple[bytes, bytes]:
    """Same as mint_cert but pulls the AD token from a credential object
    (e.g. azure.identity.DefaultAzureCredential or ManagedIdentityCredential).
    Convenience wrapper used by container bootstrap.py."""
    token_obj = mi_credential.get_token("https://management.azure.com/.default")
    return mint_cert(subject, token_obj.token, caller_principal=caller_principal)


# ===========================================================================
# CA chain loaders (public certs)
# ===========================================================================
def load_ca_public_cert() -> bytes:
    """Return the intermediate CA cert PEM - the direct issuer of leaves.

    Prefers the disk copy baked at image build time. Falls back to KV
    read via managed identity (dev-only path; production images always
    have the chain on disk)."""
    if os.path.exists(CA_INTERMEDIATE_CERT_PATH):
        with open(CA_INTERMEDIATE_CERT_PATH, "rb") as f:
            return f.read()
    return _kv_read_public_cert(_KV_INTERMEDIATE_CERT)


def load_intermediate_chain() -> bytes:
    """Return intermediate || root PEM, in that order - the chain a
    consumer needs to verify a leaf back to the ChatHealthy root."""
    intermediate = load_ca_public_cert()
    if os.path.exists(CA_ROOT_CERT_PATH):
        with open(CA_ROOT_CERT_PATH, "rb") as f:
            root = f.read()
    else:
        root = _kv_read_public_cert(_KV_ROOT_CERT)
    return intermediate + b"\n" + root


def _kv_read_public_cert(secret_name: str) -> bytes:
    """Dev-fallback KV read of a public cert. Requires KEY_VAULT_URI env
    and either azure.identity or an IMDS-reachable token endpoint."""
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient
    vault_uri = os.environ.get(
        "KEY_VAULT_URI", "https://kv-chpipeline-dev.vault.azure.net/"
    )
    cred = DefaultAzureCredential()
    client = SecretClient(vault_url=vault_uri, credential=cred)
    return (client.get_secret(secret_name).value or "").encode("ascii")
