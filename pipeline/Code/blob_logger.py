"""Blob-appending logging handler.

Historical: this module previously added a stdlib logging.Handler subclass
(BlobAppendHandler) to the stdlib root logger so pipeline containers wrote
per-day .log blobs. That architecture is superseded by
ChatHealthyLoggingService (CHLS) + its MongoLogHandler, which is the single
canonical logging surface for the project (Rule-005). CHLS's MongoLogHandler
writes each record to Pipelines.Log_{env}; the file/stderr handler mirrors
it for job-stream visibility.

install() now exists only to (a) preserve the callsite import in
bootstrap.py, and (b) force CHLS to configure itself so the first log call
in the pipeline component has handlers wired.
"""
from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


def install(pipeline_name: str, level: int | None = None) -> None:
    """Force CHLS to configure itself for this pipeline component.

    level is accepted for backward compatibility with callers that pass it;
    the actual level is driven by the CH_LOG_LEVEL env var per
    logging_service._compute_level().
    """
    ChatHealthyLoggingService().info(
        "blob_logger.install: CHLS configured for pipeline_name=%s", pipeline_name
    )
