"""End-to-end test: X.509 mTLS identity-based MongoDB authentication.

Tests the complete flow:
1. Identity (pipelineEditor) → Key Vault
2. Fetch client cert (certs-pipelineEditor) + MongoDB public key (cert-mongo-publicKey)
3. Build mTLS URI
4. Connect to MongoDB with X.509 auth
"""

import os
import tempfile
from unittest.mock import Mock, patch, MagicMock
import pytest

from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities
from chathealthy_frontend_lib.exceptions import ChatHealthyException


@pytest.fixture
def mock_kv_secrets():
    """Mock Key Vault secrets for end-to-end test."""
    return {
        "certs-pipelineEditor": "-----BEGIN CERTIFICATE-----\nMOCK_CLIENT_CERT\n-----END CERTIFICATE-----",
        "cert-mongo-publicKey": "-----BEGIN CERTIFICATE-----\nMOCK_MONGO_PUBLIC_KEY\n-----END CERTIFICATE-----",
    }


@pytest.fixture
def mock_secret_client(mock_kv_secrets):
    """Mock SecretClient that returns test secrets."""
    mock_client = MagicMock()

    def get_secret_impl(name):
        secret = Mock()
        secret.value = mock_kv_secrets.get(name)
        if secret.value is None:
            return None
        return secret

    mock_client.get_secret.side_effect = get_secret_impl
    return mock_client


def test_e2e_identity_to_mongodb_connection(mock_secret_client):
    """End-to-end: pipelineEditor identity → mTLS connection to MongoDB."""
    with patch("chathealthy_frontend_lib.mongo_utilities.SecretClient", return_value=mock_secret_client):
        with patch("chathealthy_frontend_lib.mongo_utilities.DefaultAzureCredential"):
            with patch.dict(os.environ, {"KEY_VAULT_URI": "https://test-vault.vault.azure.net/"}):
                with patch("chathealthy_frontend_lib.mongo_utilities.MongoClient") as mock_mongo:
                    utilities = ChatHealthyMongoUtilities()
                    connection = utilities.getConnection("pipelineEditor")

                    # Verify both secrets were fetched
                    calls = [call[0][0] for call in mock_secret_client.get_secret.call_args_list]
                    assert "certs-pipelineEditor" in calls, "Should fetch client cert"
                    assert "cert-mongo-publicKey" in calls, "Should fetch MongoDB public key"

                    # Verify MongoClient was called with mTLS URI
                    assert mock_mongo.called, "MongoClient should be instantiated"
                    mongo_call_args = mock_mongo.call_args
                    uri_arg = mongo_call_args[0][0] if mongo_call_args[0] else None

                    assert uri_arg is not None, "URI should be passed to MongoClient"
                    assert "MONGODB-X509" in uri_arg, "URI should specify X.509 auth"
                    assert "tlsCertificateKeyFile" in uri_arg, "URI should reference client cert file"
                    assert "tlsCAFile" in uri_arg, "URI should reference MongoDB CA file"


def test_e2e_missing_client_cert_raises(mock_secret_client):
    """When client cert is missing, getConnection raises ChatHealthyException."""
    mock_secret_client.get_secret.side_effect = lambda name: None if name == "certs-pipelineEditor" else Mock(value="key")

    with patch("chathealthy_frontend_lib.mongo_utilities.SecretClient", return_value=mock_secret_client):
        with patch("chathealthy_frontend_lib.mongo_utilities.DefaultAzureCredential"):
            with patch.dict(os.environ, {"KEY_VAULT_URI": "https://test-vault.vault.azure.net/"}):
                utilities = ChatHealthyMongoUtilities()

                with pytest.raises(ChatHealthyException) as exc_info:
                    utilities.getConnection("pipelineEditor")

                assert "certs-pipelineEditor" in str(exc_info.value.message)


def test_e2e_missing_mongodb_public_key_raises(mock_secret_client):
    """When MongoDB public key is missing, getConnection raises ChatHealthyException."""
    mock_secret_client.get_secret.side_effect = lambda name: None if name == "cert-mongo-publicKey" else Mock(value="cert")

    with patch("chathealthy_frontend_lib.mongo_utilities.SecretClient", return_value=mock_secret_client):
        with patch("chathealthy_frontend_lib.mongo_utilities.DefaultAzureCredential"):
            with patch.dict(os.environ, {"KEY_VAULT_URI": "https://test-vault.vault.azure.net/"}):
                utilities = ChatHealthyMongoUtilities()

                with pytest.raises(ChatHealthyException) as exc_info:
                    utilities.getConnection("pipelineEditor")

                assert "cert-mongo-publicKey" in str(exc_info.value.message)


def test_e2e_discrepancy_report_connects_with_identity():
    """DiscrepancyReport establishes MongoDB connection using pipelineEditor identity."""
    with patch("chathealthy_frontend_lib.mongo_utilities.SecretClient") as mock_kv:
        with patch("chathealthy_frontend_lib.mongo_utilities.DefaultAzureCredential"):
            with patch.dict(os.environ, {"KEY_VAULT_URI": "https://test-vault.vault.azure.net/"}):
                with patch("chathealthy_frontend_lib.mongo_utilities.MongoClient"):
                    from pipeline.Code.steps.discrepancy_report import DiscrepancyReport

                    # Mock Key Vault to return test certs
                    mock_client = MagicMock()

                    def get_secret_impl(name):
                        secret = Mock()
                        if name == "certs-pipelineEditor":
                            secret.value = "-----BEGIN CERTIFICATE-----\nCLIENT\n-----END CERTIFICATE-----"
                        elif name == "cert-mongo-publicKey":
                            secret.value = "-----BEGIN CERTIFICATE-----\nMONGO\n-----END CERTIFICATE-----"
                        elif name == "notification-to-email":
                            secret.value = "ops@example.com"
                        else:
                            return None
                        return secret

                    mock_client.get_secret.side_effect = get_secret_impl
                    mock_kv.return_value = mock_client

                    # Create DiscrepancyReport - should establish connection via pipelineEditor identity
                    report = DiscrepancyReport(
                        run_id="test-run-001",
                        env="dev",
                        pipeline_name="test-pipeline",
                        source="test-source",
                    )

                    # Verify connection was established (not None)
                    assert report.mongo_connection is not None, "DiscrepancyReport should establish MongoDB connection"
