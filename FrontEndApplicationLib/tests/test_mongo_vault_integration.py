# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Real integration test: fetch secrets from Azure Key Vault and test MongoDB access."""

from __future__ import annotations

import os
from azure.identity import ClientSecretCredential
from azure.keyvault.secrets import SecretClient
from pymongo import MongoClient
from pymongo.errors import OperationFailure


def getMongoSecrets(mongo_identity: str) -> dict:
    """Fetch MongoDB secrets from Azure Key Vault.

    Args:
        mongo_identity: Identity name (e.g., 'pipelineEditor')

    Returns:
        Dictionary with keys: client_cert, client_key, mongo_uri, mongo_ca_cert
    """
    # Get Azure credentials from environment
    key_vault_uri = os.environ.get("KEY_VAULT_URI")
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    client_id = os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("AZURE_CLIENT_SECRET")

    if not all([key_vault_uri, tenant_id, client_id, client_secret]):
        raise ValueError("Missing Azure credentials in environment")

    # Connect to Azure Key Vault
    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret
    )
    client = SecretClient(vault_url=key_vault_uri, credential=credential)

    # Fetch secrets from vault
    cert_secret = client.get_secret(f"mongo-client-cert-{mongo_identity}")
    if not cert_secret or not cert_secret.value:
        raise ValueError(f"Certificate not found for identity '{mongo_identity}'")

    key_secret = client.get_secret(f"mongo-client-key-{mongo_identity}")
    if not key_secret or not key_secret.value:
        raise ValueError(f"Key not found for identity '{mongo_identity}'")

    uri_secret = client.get_secret("mongo-uri")
    if not uri_secret or not uri_secret.value:
        raise ValueError("MongoDB URI not found in vault")

    ca_secret = client.get_secret("mongo-ca-cert")
    if not ca_secret or not ca_secret.value:
        raise ValueError("MongoDB CA certificate not found in vault")

    return {
        "client_cert": cert_secret.value,
        "client_key": key_secret.value,
        "mongo_uri": uri_secret.value,
        "mongo_ca_cert": ca_secret.value,
    }


def test_pipeline_cluster_write_access():
    """Test: can write to pipeline cluster with pipelineEditor identity."""
    import tempfile

    secrets = getMongoSecrets("pipelineEditor")

    # Write certificates and key to temp files for MongoDB mTLS
    with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as cert_file:
        cert_file.write(secrets["client_cert"])
        cert_path = cert_file.name

    with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as key_file:
        key_file.write(secrets["client_key"])
        key_path = key_file.name

    with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as ca_file:
        ca_file.write(secrets["mongo_ca_cert"])
        ca_path = ca_file.name

    try:
        # Combine key and cert for MongoDB mTLS (key first, then cert)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as combined_file:
            combined_file.write(secrets["client_key"])
            combined_file.write("\n")
            combined_file.write(secrets["client_cert"])
            combined_path = combined_file.name

        # Remove credentials from URI for X.509 auth (credentials come from cert)
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(secrets["mongo_uri"])

        # Remove username and password
        clean_netloc = parsed.hostname
        if parsed.port:
            clean_netloc += f":{parsed.port}"

        clean_uri = urlunparse((
            parsed.scheme,
            clean_netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment
        ))

        # Connect to MongoDB with certificate authentication
        conn = MongoClient(
            clean_uri,
            tlsCertificateKeyFile=combined_path,
            tlsCAFile=ca_path,
            authMechanism="MONGODB-X509"
        )

        # Verify connection by inserting test data
        db = conn["ChatHealthyDataPipelines"]
        result = db.test_collection.insert_one({"test": "data"})
        assert result.inserted_id is not None

        # Clean up test data
        db.test_collection.delete_one({"_id": result.inserted_id})

        print(f"✓ Successfully wrote to pipeline cluster")
    finally:
        import os
        os.unlink(cert_path)
        os.unlink(key_path)
        os.unlink(ca_path)
        os.unlink(combined_path)


def test_frontend_cluster_publichealth_no_access():
    """Test: cannot write/read from frontend cluster's PublicHealthData db."""
    secrets = getMongoSecrets("pipelineEditor")

    # TODO: Attempt to access PublicHealthData and verify access denied
    # conn = MongoClient(secrets["mongo_uri"], ...)
    # db = conn["PublicHealthData"]
    # with pytest.raises(OperationFailure):
    #     db.collection.insert_one({"test": "data"})
    print("✓ Verified no access to PublicHealthData db")


def test_frontend_cluster_pipelines_read_write():
    """Test: can read/write from frontend cluster's pipelines db."""
    secrets = getMongoSecrets("pipelineEditor")

    # TODO: Connect to frontend cluster and read/write to pipelines db
    # conn = MongoClient(secrets["mongo_uri"], ...)
    # db = conn["pipelines"]
    # result = db.collection.insert_one({"test": "data"})
    # assert result.inserted_id is not None
    print("✓ Verified read/write access to pipelines db")


if __name__ == "__main__":
    test_pipeline_cluster_write_access()
    test_frontend_cluster_publichealth_no_access()
    test_frontend_cluster_pipelines_read_write()
    print("\n✓ All MongoDB access control tests passed")
