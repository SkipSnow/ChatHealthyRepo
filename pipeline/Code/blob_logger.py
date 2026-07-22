"""Blob-appending logging handler.

Historical: this module previously added a stdlib logging.Handler subclass
(BlobAppendHandler) to the stdlib root logger so pipeline containers wrote
per-day .log blobs. That architecture is superseded by
ChatHealthyLoggingService (CHLS) + its MongoLogHandler, which is the single
canonical logging surface for the project (Rule-005). CHLS's MongoLogHandler
writes each record to Pipelines.Log_{env}; the file/stderr handler mirrors
it for job-stream visibility.

install() is now a bare no-op that preserves the bootstrap.py callsite.
It MUST NOT touch CHLS: bootstrap calls install() BEFORE it loads the
KV secrets that populate MONGO_FRONTEND_connectionString / CH_SPACE_NAME
into os.environ. Firing CHLS here would race the observability gate on
those env bindings and abort bootstrap before the abort itself can log.
"""


def install(pipeline_name: str, level: int | None = None) -> None:
    """Bare no-op. See module docstring: must not touch CHLS."""
    return None
