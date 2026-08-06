"""E2E Test: Certificates valid for 1 year (REQ-B-001)"""

from __future__ import annotations

import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '../../.env'))

import sys
import pytest
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../Code/Shared/ops/tools'))
from chathealthy_cert_authority import ChatHealthyCertificateAuthority


@pytest.fixture
def vault_url():
    url = os.environ.get("AZURE_KEYVAULT_URL")
    if not url:
        pytest.skip("AZURE_KEYVAULT_URL not set")
    return url


def _create_test_root_cert(vault_url, cert_name, key_name):
    """Helper to create and store test root cert in vault."""
    credential = DefaultAzureCredential()
    client = SecretClient(vault_url=vault_url, credential=credential)

    root_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    now = datetime.now(timezone.utc)
    root_cert = x509.CertificateBuilder().subject_name(
        x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "ChatHealthy Test Root CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ChatHealthy Test"),
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        ])
    ).issuer_name(
        x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "ChatHealthy Test Root CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ChatHealthy Test"),
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        ])
    ).public_key(root_key.public_key()).serial_number(
        x509.random_serial_number()
    ).not_valid_before(now).not_valid_after(
        now + timedelta(days=365)
    ).add_extension(
        x509.BasicConstraints(ca=True, path_length=0), critical=True,
    ).sign(root_key, hashes.SHA256(), backend=default_backend())

    cert_pem = root_cert.public_bytes(serialization.Encoding.PEM).decode('utf-8')
    key_pem = root_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    ).decode('utf-8')

    client.set_secret(cert_name, cert_pem)
    client.set_secret(key_name, key_pem)


def test_certificates_valid_one_year(vault_url):
    """Test: Issued certificates are valid for exactly 1 year."""
    cert_name = "test-ca-root-cert-req-s004"
    key_name = "test-ca-root-privatekey-req-s004"

    try:
        _create_test_root_cert(vault_url, cert_name, key_name)

        ca = ChatHealthyCertificateAuthority(
            vault_url=vault_url,
            root_cert_name=cert_name,
            root_key_name=key_name
        )

        # Issue certificate
        subject_dn = "C=US,ST=California,L=SF,O=ChatHealthy,CN=validity-test.example.com"
        cert_pem, _ = ca.issue_certificate(subject_dn)

        # Load and verify certificate
        issued_cert = x509.load_pem_x509_certificate(
            cert_pem.encode('utf-8'),
            default_backend()
        )

        # Check validity period is ~1 year
        not_before = issued_cert.not_valid_before_utc
        not_after = issued_cert.not_valid_after_utc
        validity_days = (not_after - not_before).days

        # Allow 1 day tolerance for clock skew
        assert 364 <= validity_days <= 366, f"Validity period is {validity_days} days, expected ~365"

        # Cleanup
        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=vault_url, credential=credential)
        dn_hash = ca._sanitize_dn_for_vault(subject_dn)
        try:
            client.begin_delete_secret(f"cert-{dn_hash}")
            client.begin_delete_secret(f"key-{dn_hash}")
        except:
            pass

    finally:
        # Cleanup root cert
        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=vault_url, credential=credential)
        try:
            client.begin_delete_secret(cert_name)
            client.begin_delete_secret(key_name)
        except:
            pass
