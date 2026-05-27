# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""staging - read/write/delete helpers for the per-record staging blob container.

Layout: pipeline-staging/{load_id}/{assignment_id}/{worker_id}/{npi}.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from blob_client import get_blob_service


STAGING_CONTAINER = "pipeline-staging"

# Module-level singleton. _container_client() previously called create_container
# on every invocation — that's 150+ wasted Storage HTTP calls per activity
# (one per staging_write/read/delete). Cache the client; ensure the container
# exists exactly once per process.
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
            pass  # already exists is fine; auth errors will surface on first real op
        _CC_CLIENT = client
        return _CC_CLIENT


def staging_path(load_id: str, assignment_id, worker_id, npi: str) -> str:
    return f"{load_id}/{assignment_id}/{worker_id}/{npi}.json"


def staging_write(load_id: str, assignment_id, worker_id, npi: str, doc: dict) -> str:
    path = staging_path(load_id, assignment_id, worker_id, npi)
    payload = json.dumps(doc).encode("utf-8")
    metadata = {
        "load_id": str(load_id),
        "assignment_id": str(assignment_id),
        "worker_id": str(worker_id),
        "npi": str(npi),
        "staged_at": datetime.now(timezone.utc).isoformat(),
    }
    _container_client().upload_blob(
        name=path, data=payload, overwrite=True, metadata=metadata,
    )
    return path


def staging_list_for_worker(load_id: str, assignment_id, worker_id):
    prefix = f"{load_id}/{assignment_id}/{worker_id}/"
    return _container_client().list_blobs(name_starts_with=prefix)


def staging_read(path: str) -> dict:
    blob = _container_client().get_blob_client(path)
    return json.loads(blob.download_blob().readall())


def staging_delete(path: str) -> None:
    _container_client().delete_blob(path)


def staging_delete_prefix(load_id: str) -> int:
    prefix = f"{load_id}/"
    client = _container_client()
    deleted = 0
    for blob in client.list_blobs(name_starts_with=prefix):
        client.delete_blob(blob.name)
        deleted += 1
    return deleted
