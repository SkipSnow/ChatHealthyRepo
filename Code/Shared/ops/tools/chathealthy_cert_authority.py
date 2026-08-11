"""ChatHealthy Certificate Authority - X.509 certificate generation and management.

Implements EPIC-008-F-004: ChatHealthy PKI Certificate Management

Provides a client library for issuing X.509 certificates on demand. Certificates
are signed with a root CA certificate and valid for 1 year. Storage and retrieval
are vault-backed via Azure Key Vault.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtensionOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

import sys
sys.path.insert(0, "ChatHealthyLib/src")
from chathealthy_lib.exceptions import ChatHealthyException


class ChatHealthyCertificateAuthority:
    """X.509 Certificate Authority for ChatHealthy.

    Issues certificates on demand to authorized requesters. All certificates
    are signed with a root CA certificate and valid for 1 year.
    """

    def __init__(self, vault_url: str, root_cert_name: str = "ca-root-cert",
                 root_key_name: str = "ca-root-privatekey"):
        """Initialize the Certificate Authority.

        Args:
            vault_url: Azure Key Vault URL (e.g., https://<vault>.vault.azure.net/)
            root_cert_name: Name of root CA certificate in vault
            root_key_name: Name of root CA private key in vault
        """
        self.vault_url = vault_url
        self.root_cert_name = root_cert_name
        self.root_key_name = root_key_name

        # Initialize vault client
        credential = DefaultAzureCredential()
        self.secret_client = SecretClient(vault_url=vault_url, credential=credential)

        self._root_cert = None
        self._root_key = None

    def _load_root_cert(self) -> x509.Certificate:
        """Load root CA certificate from vault."""
        if self._root_cert is not None:
            return self._root_cert

        cert_pem = self.secret_client.get_secret(self.root_cert_name).value
        self._root_cert = x509.load_pem_x509_certificate(
            cert_pem.encode('utf-8'),
            default_backend()
        )
        return self._root_cert

    def _load_root_key(self) -> rsa.RSAPrivateKey:
        """Load root CA private key from vault."""
        if self._root_key is not None:
            return self._root_key

        key_pem = self.secret_client.get_secret(self.root_key_name).value
        self._root_key = serialization.load_pem_private_key(
            key_pem.encode('utf-8'),
            password=None,
            backend=default_backend()
        )
        return self._root_key

    def issue_certificate(self, subject_dn: str, common_name: Optional[str] = None) -> Tuple[str, str]:
        """Issue an X.509 certificate signed by the root CA.

        Args:
            subject_dn: Subject Distinguished Name (e.g.,
                "C=US,ST=California,L=San Francisco,O=ChatHealthy,CN=example.com")
            common_name: Optional override for Common Name (CN) field

        Returns:
            Tuple of (certificate_pem, private_key_pem)

        Raises:
            ValueError: If subject_dn is invalid
            Exception: On vault or cryptographic errors
        """
        # Parse subject DN
        try:
            subject = self._parse_dn(subject_dn)
        except Exception as e:
            raise ChatHealthyException(
                mode="security_violation",
                message=f"Invalid subject DN: {subject_dn}",
                component="ChatHealthyCertificateAuthority",
            ) from e

        # Load root CA cert and key
        root_cert = self._load_root_cert()
        root_key = self._load_root_key()

        # Generate new private key for the issued certificate
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend()
        )

        # Create certificate builder
        now = datetime.now(timezone.utc)
        cert_builder = x509.CertificateBuilder()
        cert_builder = cert_builder.subject_name(subject)
        cert_builder = cert_builder.issuer_name(root_cert.subject)
        cert_builder = cert_builder.public_key(private_key.public_key())
        cert_builder = cert_builder.serial_number(x509.random_serial_number())
        cert_builder = cert_builder.not_valid_before(now)
        cert_builder = cert_builder.not_valid_after(now + timedelta(days=365))

        # Add extensions
        cert_builder = cert_builder.add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        cert_builder = cert_builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )

        # Sign the certificate with root CA key
        certificate = cert_builder.sign(
            root_key,
            hashes.SHA256(),
            backend=default_backend()
        )

        # Serialize to PEM
        cert_pem = certificate.public_bytes(
            serialization.Encoding.PEM
        ).decode('utf-8')

        key_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ).decode('utf-8')

        return cert_pem, key_pem

    def store_certificate(self, subject_dn: str, certificate_pem: str,
                         private_key_pem: str) -> None:
        """Store issued certificate and key in vault.

        Args:
            subject_dn: Subject DN used as vault key identifier
            certificate_pem: PEM-encoded certificate
            private_key_pem: PEM-encoded private key
        """
        # Use DN hash as vault key name (sanitized)
        dn_hash = self._sanitize_dn_for_vault(subject_dn)

        # Store cert
        self.secret_client.set_secret(
            f"cert-{dn_hash}",
            certificate_pem
        )

        # Store key (separate, access controlled)
        self.secret_client.set_secret(
            f"key-{dn_hash}",
            private_key_pem
        )


    def _parse_dn(self, dn_string: str) -> x509.Name:
        """Parse a DN string into x509.Name object.

        Supports format: C=US,ST=State,L=City,O=Org,CN=Common Name
        """
        attributes = []
        for rdn in dn_string.split(','):
            key, value = rdn.strip().split('=', 1)
            key = key.strip().upper()
            value = value.strip()

            oid_map = {
                'C': NameOID.COUNTRY_NAME,
                'ST': NameOID.STATE_OR_PROVINCE_NAME,
                'L': NameOID.LOCALITY_NAME,
                'O': NameOID.ORGANIZATION_NAME,
                'OU': NameOID.ORGANIZATIONAL_UNIT_NAME,
                'CN': NameOID.COMMON_NAME,
            }

            if key not in oid_map:
                raise ChatHealthyException(
                    mode="security_violation",
                    message=f"Unknown DN attribute: {key}",
                    component="ChatHealthyCertificateAuthority",
                )

            attributes.append(x509.NameAttribute(oid_map[key], value))

        return x509.Name(attributes)

    def _sanitize_dn_for_vault(self, dn_string: str) -> str:
        """Sanitize DN string to valid vault secret name.

        Vault secret names: alphanumeric, hyphen, underscore only.
        """
        import hashlib

        # Use SHA256 hash of DN for deterministic, safe vault key
        dn_hash = hashlib.sha256(dn_string.encode()).hexdigest()[:16]
        return dn_hash
