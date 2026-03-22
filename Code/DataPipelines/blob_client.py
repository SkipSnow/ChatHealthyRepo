# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""Shared BlobServiceClient singleton — one HTTP connection pool per worker process.

Import get_blob_service() in place of BlobServiceClient.from_connection_string()
anywhere in the pipeline. Follows the same pattern as _get_mongo_client().
"""

import os

from azure.storage.blob import BlobServiceClient

_blob_service: BlobServiceClient | None = None


def get_blob_service() -> BlobServiceClient:
    global _blob_service
    if _blob_service is None:
        _blob_service = BlobServiceClient.from_connection_string(
            os.environ["AZURE_STORAGE_CONNECTION_STRING"]
        )
    return _blob_service
