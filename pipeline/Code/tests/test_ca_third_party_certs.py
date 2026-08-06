"""E2E Test: Vault supports third-party certificates (REQ-B-003)"""

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


def test_vault_supports_third_party_certificates(vault_url):
    """Test: Vault can store public certificates from other authorities."""
    cert_name = "test-ca-root-cert-req-s005b3"
    key_name = "test-ca-root-privatekey-req-s005b3"

    try:
        _create_test_root_cert(vault_url, cert_name, key_name)

        ca = ChatHealthyCertificateAuthority(
            vault_url=vault_url,
            root_cert_name=cert_name,
            root_key_name=key_name
        )

        # Create a third-party (external) certificate signed by a different authority
        external_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend()
        )

        now = datetime.now(timezone.utc)
        external_cert = x509.CertificateBuilder().subject_name(
            x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, "external-ca.example.com"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "External Authority"),
                x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            ])
        ).issuer_name(
            x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, "External Root CA"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "External Authority"),
                x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            ])
        ).public_key(external_key.public_key()).serial_number(
            x509.random_serial_number()
        ).not_valid_before(now).not_valid_after(
            now + timedelta(days=365)
        ).add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        ).sign(external_key, hashes.SHA256(), backend=default_backend())

        external_cert_pem = external_cert.public_bytes(
            serialization.Encoding.PEM
        ).decode('utf-8')

        # Store third-party certificate in vault via CA storage mechanism
        third_party_dn = "C=US,O=External Authority,CN=external-ca.example.com"

        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=vault_url, credential=credential)

        # Store the external certificate using vault's direct secret API
        dn_hash = ca._sanitize_dn_for_vault(third_party_dn)
        client.set_secret(f"third-party-{dn_hash}", external_cert_pem)

        # Retrieve and verify it's stored
        retrieved_third_party = client.get_secret(f"third-party-{dn_hash}").value
        assert retrieved_third_party == external_cert_pem

        # Verify it loads as valid certificate
        loaded_external = x509.load_pem_x509_certificate(
            retrieved_third_party.encode('utf-8'),
            default_backend()
        )
        assert loaded_external is not None
        assert "External Root CA" in loaded_external.issuer.rfc4514_string()

        # Cleanup
        try:
            client.begin_delete_secret(f"third-party-{dn_hash}")
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
