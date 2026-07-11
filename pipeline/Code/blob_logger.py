"""Blob-appending logging handler.

Every container that runs a pipeline stage logs to the same daily blob:
    <account>/pipeline-logs/<pipeline_name>/<yyyy-mm-dd>.log

Uses Azure Blob append-blob type so concurrent Control + Worker containers
can all write to the same day's log without stomping each other. Auth via
attached managed identity (DefaultAzureCredential).
"""
from __future__ import annotations

import datetime
import logging
import os
import socket
import sys

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, BlobType


LOG_ACCOUNT_URL = os.environ.get(
    "PIPELINE_LOG_ACCOUNT_URL", "https://stchpipelinedev.blob.core.windows.net"
)
LOG_CONTAINER = os.environ.get("PIPELINE_LOG_CONTAINER", "pipeline-logs")


class BlobAppendHandler(logging.Handler):
    """Append each formatted log record to the daily blob. Creates the
    append blob on first write; opens for append on every subsequent
    write. Cheap enough — one HTTP call per record. If ordering across
    concurrent writers matters, a queue is the alternative; for a
    pipeline log this is fine."""

    def __init__(self, pipeline_name: str, level: int = logging.NOTSET):
        super().__init__(level)
        self.pipeline_name = pipeline_name
        self._hostname = socket.gethostname()
        self._instance = os.environ.get(
            "CONTAINER_APP_JOB_EXECUTION_NAME", self._hostname
        )
        self._client = BlobServiceClient(
            account_url=LOG_ACCOUNT_URL, credential=DefaultAzureCredential()
        )
        self._container = self._client.get_container_client(LOG_CONTAINER)
        self._current_date = None
        self._current_blob = None

    def _blob_for_today(self):
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        if today == self._current_date and self._current_blob is not None:
            return self._current_blob
        blob_name = f"{self.pipeline_name}/{today}.log"
        blob = self._container.get_blob_client(blob_name)
        # Create the append blob if it doesn't exist yet.
        try:
            blob.create_append_blob()
        except Exception:
            pass  # already exists — fine
        self._current_date = today
        self._current_blob = blob
        return blob

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            # Prepend the instance tag so operators can trace which container.
            line = f"[{self._instance}] {msg}\n"
            self._blob_for_today().append_block(line.encode("utf-8"))
        except Exception:
            # Don't kill the pipeline if logging fails; just print.
            print(f"BlobAppendHandler emit failed: {record.getMessage()}",
                  file=sys.stderr)


def install(pipeline_name: str, level: int = logging.INFO) -> None:
    """Wire the BlobAppendHandler into the root logger AND a StreamHandler
    for stdout (so ACA console logs still show up in Log Analytics)."""
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    for h in list(root.handlers):
        root.removeHandler(h)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    blob = BlobAppendHandler(pipeline_name)
    blob.setFormatter(fmt)
    root.addHandler(blob)
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    root.info(f"blob_logger installed for {pipeline_name}")
