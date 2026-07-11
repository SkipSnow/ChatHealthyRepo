# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""staging - one-file-per-assignment helpers for the staging blob container.

Layout: pipeline-staging/{load_id}/{worker_id}/{chunk_id}.json

Each worker has its own directory under the load_id. Inside that
directory, each assignment the worker handles is one blob — the
serialized list of provider documents the worker built for that chunk.
The worker writes the file once at commit time, bulk_writes Mongo, then
deletes the file. One write, one delete per assignment. Write key ===
delete key, so there is no per-NPI explosion and no asymmetry between
what was written and what is being deleted.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from blob_client import get_blob_service


STAGING_CONTAINER = "pipeline-staging"

import threading as _threading_for_staging
_CC_CLIENT = None
_CC_INIT_LOCK = _threading_for_staging.Lock()


def _container_client():
    global _CC_CLIENT
    if _CC_CLIENT is not None:
        return _CC_CLIENT
    with _CC_INIT_LOCK:
        if _CC_CLIENT is not None:
            return _CC_CLIENT
        svc = get_blob_service()
        client = svc.get_container_client(STAGING_CONTAINER)
        try:
            client.create_container()
        except Exception:
            pass  # already exists is fine
        _CC_CLIENT = client
        return _CC_CLIENT


def staging_path(load_id: str, worker_id, chunk_id) -> str:
    return f"{load_id}/{worker_id}/{chunk_id}.json"


def staging_write_batch(load_id: str, worker_id, chunk_id, docs: list[dict]) -> str:
    """Write the whole batch as a single blob. Returns the blob path."""
    path = staging_path(load_id, worker_id, chunk_id)
    payload = json.dumps(docs).encode("utf-8")
    metadata = {
        "load_id": str(load_id),
        "worker_id": str(worker_id),
        "chunk_id": str(chunk_id),
        "doc_count": str(len(docs)),
        "staged_at": datetime.now(timezone.utc).isoformat(),
    }
    _container_client().upload_blob(
        name=path, data=payload, overwrite=True, metadata=metadata,
    )
    return path


def staging_delete(path: str) -> None:
    _container_client().delete_blob(path)
