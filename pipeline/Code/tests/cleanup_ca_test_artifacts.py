"""Cleanup script for CA test artifacts in Azure Key Vault.

Removes all test certificates and keys created by CA tests.
Run this script to clean up the vault after running tests.

Usage:
    python cleanup_ca_test_artifacts.py
"""

import os
from dotenv import load_dotenv

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "FrontEndApplicationLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()

load_dotenv(os.path.join(os.path.dirname(__file__), '../../.env'))

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient


def cleanup_test_artifacts():
    """Delete all test CA artifacts from vault."""
    vault_url = os.environ.get("AZURE_KEYVAULT_URL")
    if not vault_url:
        _CH_LOG.info("ERROR: AZURE_KEYVAULT_URL not set in .env")
        return False

    credential = DefaultAzureCredential()
    client = SecretClient(vault_url=vault_url, credential=credential)

    # Test secret name patterns
    test_patterns = [
        "test-ca-root-cert-",
        "test-ca-root-privatekey-",
        "cert-",
        "key-",
        "third-party-",
    ]

    deleted_count = 0
    error_count = 0

    # List all secrets and delete test ones
    for secret_props in client.list_properties_of_secrets():
        secret_name = secret_props.name

        # Check if this is a test artifact
        is_test = any(pattern in secret_name for pattern in test_patterns)

        if is_test:
            try:
                # Delete the secret
                client.begin_delete_secret(secret_name)
                _CH_LOG.info(f"✓ Deleted: {secret_name}")
                deleted_count += 1
            except Exception as e:
                _CH_LOG.info(f"✗ Failed to delete {secret_name}: {e}")
                error_count += 1

    _CH_LOG.info(f"\nCleanup complete: {deleted_count} secrets deleted, {error_count} errors")
    return error_count == 0


if __name__ == "__main__":
    success = cleanup_test_artifacts()
    exit(0 if success else 1)
