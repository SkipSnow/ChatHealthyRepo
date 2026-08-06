"""E2E Test: CA accepts requests from any IP with distinguished name (REQ-B-001)"""

from __future__ import annotations

import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '../../.env'))

import pytest
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

# Add the tools path to Python path for imports
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../Code/Shared/ops/tools'))

from chathealthy_cert_authority import ChatHealthyCertificateAuthority


@pytest.fixture
def vault_url():
    """Get vault URL from environment."""
    url = os.environ.get("AZURE_KEYVAULT_URL")
    if not url:
        pytest.skip("AZURE_KEYVAULT_URL not set")
    return url


@pytest.fixture
def temp_root_cert_in_vault(vault_url):
    """Create a temporary root CA cert in vault for testing."""
    credential = DefaultAzureCredential()
    client = SecretClient(vault_url=vault_url, credential=credential)

    # Generate temporary root cert for testing
    root_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
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
    ).public_key(
        root_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        now
    ).not_valid_after(
        now + timedelta(days=365)
    ).add_extension(
        x509.BasicConstraints(ca=True, path_length=0),
        critical=True,
    ).sign(root_key, hashes.SHA256(), backend=default_backend())

    # Store in vault with test-specific names
    cert_pem = root_cert.public_bytes(serialization.Encoding.PEM).decode('utf-8')
    key_pem = root_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    ).decode('utf-8')

    test_root_cert_name = "test-ca-root-cert-req-b001"
    test_root_key_name = "test-ca-root-privatekey-req-b001"

    client.set_secret(test_root_cert_name, cert_pem)
    client.set_secret(test_root_key_name, key_pem)

    yield test_root_cert_name, test_root_key_name

    # Cleanup
    client.begin_delete_secret(test_root_cert_name)
    client.begin_delete_secret(test_root_key_name)


def test_ca_accepts_request_with_dn(vault_url, temp_root_cert_in_vault):
    """Test: CA accepts requests from any IP with valid distinguished name."""
    root_cert_name, root_key_name = temp_root_cert_in_vault

    # Initialize CA with test certs
    ca = ChatHealthyCertificateAuthority(
        vault_url=vault_url,
        root_cert_name=root_cert_name,
        root_key_name=root_key_name
    )

    # Request certificate with valid DN
    subject_dn = "C=US,ST=California,L=San Francisco,O=ChatHealthy,CN=test-request.example.com"
    cert_pem, key_pem = ca.issue_certificate(subject_dn)

    # Verify certificate was issued
    assert cert_pem is not None
    assert key_pem is not None
    assert "-----BEGIN CERTIFICATE-----" in cert_pem
    assert "-----BEGIN RSA PRIVATE KEY-----" in key_pem

    # Verify DN is in certificate
    cert = x509.load_pem_x509_certificate(
        cert_pem.encode('utf-8'),
        default_backend()
    )

    subject = cert.subject
    cn = subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    assert len(cn) == 1
    assert cn[0].value == "test-request.example.com"

    # Cleanup from vault
    credential = DefaultAzureCredential()
    client = SecretClient(vault_url=vault_url, credential=credential)
    dn_hash = ca._sanitize_dn_for_vault(subject_dn)

    try:
        client.begin_delete_secret(f"cert-{dn_hash}")
        client.begin_delete_secret(f"key-{dn_hash}")
    except:
        pass
