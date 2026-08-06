"""E2E Test: Certificate DN matches vault retrieval key (REQ-B-002)"""

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


def test_dn_matches_vault_retrieval_key(vault_url):
    """Test: Certificate DN matches the vault retrieval key."""
    cert_name = "test-ca-root-cert-req-s005b2"
    key_name = "test-ca-root-privatekey-req-s005b2"

    try:
        _create_test_root_cert(vault_url, cert_name, key_name)

        ca = ChatHealthyCertificateAuthority(
            vault_url=vault_url,
            root_cert_name=cert_name,
            root_key_name=key_name
        )

        # Issue certificate with specific DN
        subject_dn = "C=US,ST=California,L=SF,O=ChatHealthy,CN=dn-match-test.example.com"
        cert_pem, key_pem = ca.issue_certificate(subject_dn)

        # Store in vault
        ca.store_certificate(subject_dn, cert_pem, key_pem)

        # Generate vault key from DN
        dn_hash = ca._sanitize_dn_for_vault(subject_dn)
        vault_key = f"cert-{dn_hash}"

        # Verify key exists and contains our certificate
        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=vault_url, credential=credential)

        retrieved_cert_pem = client.get_secret(vault_key).value

        # Load certificate and verify DN
        cert = x509.load_pem_x509_certificate(
            retrieved_cert_pem.encode('utf-8'),
            default_backend()
        )

        # Extract CN from certificate
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        assert len(cn) == 1
        assert cn[0].value == "dn-match-test.example.com"

        # Same DN should always hash to same vault key (deterministic)
        dn_hash_2 = ca._sanitize_dn_for_vault(subject_dn)
        assert dn_hash == dn_hash_2

        # Cleanup
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
