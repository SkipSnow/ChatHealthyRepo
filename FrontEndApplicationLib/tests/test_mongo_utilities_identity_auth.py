"""Unit tests for MongoDB X.509 certificate authentication via identity.

Tests ChatHealthyMongoUtilities.getConnection(identity) which retrieves
MongoDB connection URIs from Azure Key Vault using the provided identity
as the secret name. The URI includes X.509 certificate details for mTLS.
"""

import os
from unittest.mock import Mock, patch, MagicMock
import pytest

from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities
from chathealthy_frontend_lib.exceptions import ChatHealthyException


@pytest.fixture
def mock_key_vault_secret():
    """Mock Azure Key Vault secret containing MongoDB URI."""
    secret = Mock()
    secret.value = "mongodb+srv://cluster.mongodb.net/?authSource=%24external&authMechanism=MONGODB-X509&retryWrites=true&w=majority"
    return secret


@pytest.fixture
def mock_secret_client(mock_key_vault_secret):
    """Mock Azure SecretClient."""
    mock_client = MagicMock()
    mock_client.get_secret.return_value = mock_key_vault_secret
    return mock_client


def test_get_connection_with_identity_fetches_from_vault(mock_secret_client):
    """getConnection(identity) should fetch URI from Key Vault."""
    env_vars = {
        "KEY_VAULT_URI": "https://test-vault.vault.azure.net/",
        "AZURE_TENANT_ID": "test-tenant-id",
        "AZURE_CLIENT_ID": "test-client-id",
        "AZURE_CLIENT_SECRET": "test-client-secret",
    }
    with patch("chathealthy_frontend_lib.mongo_utilities.SecretClient", return_value=mock_secret_client):
        with patch("chathealthy_frontend_lib.mongo_utilities.ClientSecretCredential"):
            with patch.dict(os.environ, env_vars):
                with patch("chathealthy_frontend_lib.mongo_utilities.MongoClient") as mock_mongo:
                    utilities = ChatHealthyMongoUtilities()
                    connection = utilities.getConnection("pipelineEditor")

                    # Verify SecretClient was called with the right vault URI
                    mock_secret_client.get_secret.assert_called_once_with("pipelineEditor")

                    # Verify MongoClient was called with the URI from the vault
                    mock_mongo.assert_called()


def test_get_connection_raises_when_identity_not_found(mock_secret_client):
    """getConnection should raise ChatHealthyException when identity not in vault."""
    mock_secret_client.get_secret.return_value = None

    with patch("chathealthy_frontend_lib.mongo_utilities.SecretClient", return_value=mock_secret_client):
        with patch("chathealthy_frontend_lib.mongo_utilities.ClientSecretCredential"):
            with patch.dict(os.environ, {"KEY_VAULT_URI": "https://test-vault.vault.azure.net/"}):
                utilities = ChatHealthyMongoUtilities()

                with pytest.raises(ChatHealthyException) as exc_info:
                    utilities.getConnection("nonexistent")

                assert exc_info.value.mode == "vault_unreachable"
                assert "not found in vault" in str(exc_info.value.message)


def test_get_connection_raises_when_key_vault_uri_not_set(mock_secret_client):
    """getConnection should raise when KEY_VAULT_URI env var is not set."""
    with patch("chathealthy_frontend_lib.mongo_utilities.SecretClient"):
        with patch("chathealthy_frontend_lib.mongo_utilities.ClientSecretCredential"):
            with patch.dict(os.environ, {}, clear=True):
                utilities = ChatHealthyMongoUtilities()

                with pytest.raises(ChatHealthyException) as exc_info:
                    utilities.getConnection("pipelineEditor")

                assert exc_info.value.mode == "vault_unreachable"
                assert "KEY_VAULT_URI not set" in str(exc_info.value.message)


def test_get_connection_raises_on_vault_access_error(mock_secret_client):
    """getConnection should raise ChatHealthyException on vault access errors."""
    mock_secret_client.get_secret.side_effect = Exception("Vault access denied")

    with patch("chathealthy_frontend_lib.mongo_utilities.SecretClient", return_value=mock_secret_client):
        with patch("chathealthy_frontend_lib.mongo_utilities.ClientSecretCredential"):
            with patch.dict(os.environ, {"KEY_VAULT_URI": "https://test-vault.vault.azure.net/"}):
                utilities = ChatHealthyMongoUtilities()

                with pytest.raises(ChatHealthyException) as exc_info:
                    utilities.getConnection("pipelineEditor")

                assert exc_info.value.mode == "vault_unreachable"
                assert "Failed to get URI for identity" in str(exc_info.value.message)
