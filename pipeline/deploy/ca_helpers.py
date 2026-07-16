"""ca_helpers.py - shared helpers for the ChatHealthy CA runbooks.

Consumed by ca_bootstrap_runbook.py and ca_endpoint_runbook.py (inlined
into each staged Azure Automation runbook at build time — AA is one
file), by operator/local smoke scripts, and mirrored conceptually by
pipeline/Code/chathealthy_ca.py. Isolates the three concerns that both
runbook files use:

  - Key Vault get/put wrappers (via IMDS-fetched token, plain urllib -
    no azure SDK dependency; runbooks execute in the Azure Automation
    Python 3 sandbox which does not always carry azure-keyvault-secrets).
  - X.509 primitives (root/intermediate CA generation, CSR verification,
    leaf cert signing) via the `cryptography` library.
  - Namespace-scoping check per F-003 Design v1 sec 4.3 - given the
    calling principal's managed-identity name and the CSR subject CN,
    decide whether the mint is permitted.

Design constraints realized here:
  - Root CA private key never leaves offline custody; the runbook path
    that reads a KV secret containing the root key is bootstrap-only.
  - Intermediate CA private key lives in KV `kv-chpipeline-dev`; only
    mi-runbook reads it (enforced by KV RBAC out of scope of this file).
  - Leaf validity: 90 days for long-lived (Runbook, Control, deploy-time),
    1 day for short-lived (Worker self-mint).
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Leaf validity per identity class (F-003 Design v1 sec 3.3 / S-006-REQ-B-004)
# ---------------------------------------------------------------------------
LEAF_VALIDITY_LONG_LIVED_DAYS = 90
LEAF_VALIDITY_SHORT_LIVED_DAYS = 1

# Root and intermediate CA validity (bootstrap ceremony)
ROOT_CA_VALIDITY_YEARS = 10
INTERMEDIATE_CA_VALIDITY_YEARS = 3

# Key sizes
CA_KEY_BITS = 4096
LEAF_MIN_KEY_BITS = 2048

# KV secret names owned by this CA
KV_SECRET_ROOT_CA_KEY = "ca-root-privatekey"
KV_SECRET_ROOT_CA_CERT = "ca-root-cert"
KV_SECRET_INTERMEDIATE_KEY = "ca-intermediate-privatekey"
KV_SECRET_INTERMEDIATE_CERT = "ca-intermediate-cert"
KV_SECRET_BOOTSTRAP_MARKER = "ca-bootstrap-complete"

# CA subject naming
CA_ORGANIZATION = "ChatHealthy.ai"
CA_ROOT_CN = "ChatHealthy Root CA"
CA_INTERMEDIATE_CN = "ChatHealthy Intermediate CA"


# ===========================================================================
# Namespace-scoping policy (F-003 Design v1 sec 4.3)
# ===========================================================================
# Map calling principal (managed-identity display name) -> permitted CSR
# subject CN pattern. The check is prefix-based over the CN, deliberately
# blunt so this file stays readable.
_NAMESPACE_POLICY = {
    "mi-runbook":  ("pipeline-runbook.",  "long-lived"),
    "mi-control":  ("pipeline-control.",  "long-lived"),
    "mi-worker":   ("pipeline-worker-",   "short-lived"),
}

# The deployer SP is permitted to mint any pipeline-*.* subject at deploy
# time. Identified by the "sp-deployer" role assignment (we look at the
# principal name; the KV RBAC layer independently enforces the SP's
# Set-only permission).
_DEPLOYER_PRINCIPAL_HINT = "sp-deployer"
_DEPLOYER_ALLOWED_PREFIXES = ("pipeline-",)


class NamespaceRefused(Exception):
    """Raised when a mint request violates the F-003 namespace policy."""


def check_namespace_allowed(caller_principal: str,
                            requested_cn: str) -> str:
    """Return the identity class ('long-lived' | 'short-lived') if the
    caller may mint the requested subject CN, else raise NamespaceRefused.

    caller_principal is the display name of the Azure AD principal that
    authenticated the request (managed-identity name or SP name).
    requested_cn is the Common Name pulled from the CSR subject.

    Per F-003 Design v1 sec 4.3:
      mi-runbook  -> pipeline-runbook.*
      mi-control  -> pipeline-control.*
      mi-worker   -> pipeline-worker-*
      sp-deployer -> pipeline-*.*  (deploy-time-provisioning set)
    """
    if not caller_principal:
        raise NamespaceRefused("caller principal is empty")
    if not requested_cn:
        raise NamespaceRefused("requested subject CN is empty")

    # Deployer SP path first (broadest scope)
    if _DEPLOYER_PRINCIPAL_HINT in caller_principal.lower():
        for prefix in _DEPLOYER_ALLOWED_PREFIXES:
            if requested_cn.startswith(prefix):
                return "long-lived"
        raise NamespaceRefused(
            f"deployer principal '{caller_principal}' may mint only "
            f"pipeline-*.* subjects; got '{requested_cn}'"
        )

    entry = _NAMESPACE_POLICY.get(caller_principal)
    if entry is None:
        raise NamespaceRefused(
            f"caller principal '{caller_principal}' is not in the "
            f"namespace policy; refusing mint of '{requested_cn}'"
        )
    prefix, klass = entry
    if not requested_cn.startswith(prefix):
        raise NamespaceRefused(
            f"caller '{caller_principal}' may mint '{prefix}*' subjects; "
            f"got '{requested_cn}'"
        )
    return klass


# ===========================================================================
# Azure AD / Key Vault helpers (IMDS-based; no azure SDK required)
# ===========================================================================
def imds_token(resource: str) -> str:
    """Fetch an OAuth2 access token for the given resource from IMDS.
    Works in Automation Runbook sandbox and any Azure compute with an
    attached managed identity."""
    identity_endpoint = os.environ.get("IDENTITY_ENDPOINT")
    identity_header = os.environ.get("IDENTITY_HEADER")
    if identity_endpoint and identity_header:
        url = f"{identity_endpoint}?resource={resource}&api-version=2019-08-01"
        req = urllib.request.Request(
            url, headers={"X-IDENTITY-HEADER": identity_header}
        )
    else:
        url = (
            "http://169.254.169.254/metadata/identity/oauth2/token"
            f"?api-version=2018-02-01&resource={resource}"
        )
        req = urllib.request.Request(url, headers={"Metadata": "true"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))["access_token"]


def kv_get(vault_uri: str, secret_name: str,
           token: Optional[str] = None) -> Optional[str]:
    """Return secret value or None if not found (404)."""
    tok = token or imds_token("https://vault.azure.net")
    url = f"{vault_uri.rstrip('/')}/secrets/{secret_name}?api-version=7.4"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))["value"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def kv_put(vault_uri: str, secret_name: str, value: str,
           token: Optional[str] = None,
           tags: Optional[dict] = None) -> None:
    tok = token or imds_token("https://vault.azure.net")
    url = f"{vault_uri.rstrip('/')}/secrets/{secret_name}?api-version=7.4"
    body = {"value": value}
    if tags:
        body["tags"] = tags
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        method="PUT",
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
        },
        data=payload,
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


# ===========================================================================
# X.509 primitives - CA generation, CSR verification, leaf signing
# ===========================================================================
def generate_ca_keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=CA_KEY_BITS)


def build_root_ca_cert(root_key: rsa.RSAPrivateKey) -> x509.Certificate:
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, CA_ORGANIZATION),
        x509.NameAttribute(NameOID.COMMON_NAME, CA_ROOT_CN),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365 * ROOT_CA_VALIDITY_YEARS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )


def build_intermediate_ca_cert(intermediate_key: rsa.RSAPrivateKey,
                               root_key: rsa.RSAPrivateKey,
                               root_cert: x509.Certificate) -> x509.Certificate:
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, CA_ORGANIZATION),
        x509.NameAttribute(NameOID.COMMON_NAME, CA_INTERMEDIATE_CN),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(root_cert.subject)
        .public_key(intermediate_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365 * INTERMEDIATE_CA_VALIDITY_YEARS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(intermediate_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )


def verify_csr(csr_pem: bytes) -> x509.CertificateSigningRequest:
    """Parse and structurally verify a CSR. Raises ValueError-family from
    the cryptography library on malformed input; raises a plain Exception
    on hygiene failure (key too small, signature mismatch)."""
    csr = x509.load_pem_x509_csr(csr_pem)
    if not csr.is_signature_valid:
        raise Exception("CSR signature does not verify against its own public key")
    pub = csr.public_key()
    if not isinstance(pub, rsa.RSAPublicKey):
        raise Exception("CSR public key must be RSA")
    if pub.key_size < LEAF_MIN_KEY_BITS:
        raise Exception(
            f"CSR public key size {pub.key_size} below minimum {LEAF_MIN_KEY_BITS}"
        )
    return csr


def extract_cn(csr: x509.CertificateSigningRequest) -> str:
    """Pull the Common Name from the CSR subject. Empty string if absent."""
    for attr in csr.subject:
        if attr.oid == NameOID.COMMON_NAME:
            return str(attr.value)
    return ""


def sign_leaf(csr: x509.CertificateSigningRequest,
              intermediate_key: rsa.RSAPrivateKey,
              intermediate_cert: x509.Certificate,
              validity_days: int) -> x509.Certificate:
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(intermediate_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True,
                content_commitment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([
                x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH,
                x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
            ]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(csr.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(intermediate_key.public_key()),
            critical=False,
        )
        .sign(intermediate_key, hashes.SHA256())
    )


def cert_to_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def key_to_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def load_key_pem(pem: bytes) -> rsa.RSAPrivateKey:
    return serialization.load_pem_private_key(pem, password=None)


def load_cert_pem(pem: bytes) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem)


def validity_days_for(identity_class: str) -> int:
    if identity_class == "short-lived":
        return LEAF_VALIDITY_SHORT_LIVED_DAYS
    if identity_class == "long-lived":
        return LEAF_VALIDITY_LONG_LIVED_DAYS
    raise Exception(f"unknown identity_class: {identity_class}")
