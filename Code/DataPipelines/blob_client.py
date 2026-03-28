# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""Shared BlobServiceClient singleton and container name helpers.

Import get_blob_service() in place of BlobServiceClient.from_connection_string()
anywhere in the pipeline. Follows the same pattern as _get_mongo_client().

Container naming:
  - admin              : business operations (no ENV_PREFIX, not lifecycle-managed)
  - {ENV_PREFIX}-brain : Brain Loop artifacts (lifecycle-managed)
  - provider-data      : NPI data, ICD-10, reports (ENV_PREFIX deferred to beta)
  - chathealthy-public-data : specialty taxonomy (ENV_PREFIX deferred to beta)
"""

import os

from azure.storage.blob import BlobServiceClient

_blob_service: BlobServiceClient | None = None

# Container names
CONTAINER_ADMIN = "admin"
CONTAINER_PROVIDER_DATA = "provider-data"
CONTAINER_PUBLIC_DATA = "chathealthy-public-data"


def _env_prefix() -> str:
    return os.getenv("ENV_PREFIX", "dev")


def container_brain() -> str:
    """Brain Loop artifacts container — lifecycle-managed via ENV_PREFIX."""
    return f"{_env_prefix()}-brain"


def get_blob_service() -> BlobServiceClient:
    global _blob_service
    if _blob_service is None:
        _blob_service = BlobServiceClient.from_connection_string(
            os.environ["AZURE_STORAGE_CONNECTION_STRING"]
        )
    return _blob_service
