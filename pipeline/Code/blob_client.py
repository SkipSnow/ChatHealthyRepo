# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
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

import requests
from requests.adapters import HTTPAdapter

from azure.core.pipeline.transport import RequestsTransport
from azure.storage.blob import BlobServiceClient

_blob_service: BlobServiceClient | None = None
_BLOB_POOL_MAXSIZE = 64

# Container names
CONTAINER_ADMIN = "admin"
CONTAINER_PUBLIC_DATA = "chathealthy-public-data"
# Provider data lives under chathealthy-public-data/provider/ (virtual folder)
# Specialty data lives at chathealthy-public-data/ root


def _env_prefix() -> str:
    return os.getenv("ENV_PREFIX", "dev")


def container_brain() -> str:
    """Brain Loop artifacts container — lifecycle-managed via ENV_PREFIX."""
    return f"{_env_prefix()}-brain"


def get_blob_service() -> BlobServiceClient:
    """Pipeline-scoped BlobServiceClient. Points at the pipeline storage
    account (stchpipelinedev per manifest target_azure_storage_account_
    pipeline), NOT the front-end storage account (findcarestorage) that
    AZURE_STORAGE_CONNECTION_STRING carries.

    Reads PIPELINE_STORAGE_CONNECTION_STRING from env. Deploy chain
    populates it from .env into KV and hydrates it into the
    Controller container via bootstrap._load_all_secrets_into_env; the
    Runbook receives the same value via its Automation Variables and
    passes it to the Controller container via docker -e."""
    global _blob_service
    if _blob_service is None:
        conn = os.environ.get("PIPELINE_STORAGE_CONNECTION_STRING", "").strip()
        if not conn:
            from chathealthy_frontend_lib.exceptions import ChatHealthyException  # noqa: PLC0415
            raise ChatHealthyException(
                mode="runtime_error",
                message=(
                    "PIPELINE_STORAGE_CONNECTION_STRING env var is not set. "
                    "This is the connection string for the pipeline storage "
                    "account (stchpipelinedev per deployment_architecture.json). "
                    "The prior code fell back to AZURE_STORAGE_CONNECTION_STRING "
                    "which points at the front-end storage account "
                    "(findcarestorage) and caused every pipeline fetch to upload "
                    "to the WRONG account. Set PIPELINE_STORAGE_CONNECTION_STRING "
                    "in .env and re-deploy."
                ),
                component="pipeline.blob_client",
            )
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=_BLOB_POOL_MAXSIZE,
            pool_maxsize=_BLOB_POOL_MAXSIZE,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _blob_service = BlobServiceClient.from_connection_string(
            conn,
            transport=RequestsTransport(session=session),
        )
    return _blob_service
