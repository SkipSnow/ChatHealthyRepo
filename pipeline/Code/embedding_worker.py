# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""EmbeddingWorker — generates OpenAI text-embedding-3-large embeddings for all
provider documents assigned to a given worker_id and stores them back to the
staging collection.

Idempotent: skips documents that already have an 'embedding' field.

Projection: uses provider_embedding.should_embed / project / render.
  - Excludes records where county.reason is 'no_address' or 'zip_state_mismatch'.
  - All other records embed regardless of county enrichment status.
  - See Analysis/embedding-design-2026-03-24.md for the full design.

Rate limiting:
  - 429 RateLimitError is retried with exponential backoff (MAX_RETRIES attempts).
  - Retry-After header is honoured when present.
  - Cursor does NOT advance on 429 — the same batch is retried.
  - Only after retry exhaustion does the batch count as a failure and get skipped.
"""

import logging
import os
import random
import time

import openai
from county_enrichment_job import _build_states_filter
from pipeline_worker_base import PipelineWorkerBase
from provider_embedding import render, project, should_embed
from pymongo import MongoClient, UpdateOne

_mongo: MongoClient | None = None
_oai: openai.OpenAI | None = None

EMBED_MODEL        = "text-embedding-3-large"  # default model
EMBED_VERSION      = "0.1"                      # bump when model or projection changes
EMBED_BATCH_SIZE   = 100                        # default; override via embed_batch_size in config

# Supported embedding models — model name → vector dimensions.
# Abend if an unsupported model is passed in config.
# Add entries here when a new model is validated end-to-end (projection + index + query).
SUPPORTED_EMBED_MODELS: dict[str, int] = {
    "text-embedding-3-large": 3072,
}
MAX_RETRIES        = 5                          # max attempts per batch before skipping
RETRY_BASE_DELAY   = 10.0                       # seconds; doubles each attempt
RETRY_MAX_DELAY    = 120.0                      # cap on computed backoff
JITTER_MAX_DEFAULT = 5.0                        # max startup jitter seconds; override via embed_initial_jitter

# MongoDB-level pre-filter — mirrors should_embed() to avoid loading excluded records.
# should_embed() is still called per-document as the authoritative gate.
_EMBED_PREFILTER = {
    "county.reason": {"$nin": ["no_address", "zip_state_mismatch"]},
    "bad_data.flagged": {"$ne": True},
    "out_of_scope.flagged": {"$ne": True},
}


def _get_mongo() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(
            os.environ["MONGO_connectionString"],
            serverSelectionTimeoutMS=120_000,
        )
    return _mongo


def _get_openai() -> openai.OpenAI:
    global _oai
    if _oai is None:
        _oai = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _oai


def _retry_after_seconds(exc: openai.RateLimitError) -> float | None:
    """Extract Retry-After header value from a RateLimitError, if present."""
    try:
        val = exc.response.headers.get("retry-after")
        return float(val) if val is not None else None
    except Exception:
        return None


class EmbeddingWorker(PipelineWorkerBase):

    def __init__(self, config: dict):
        super().__init__(config)
        self.worker_id = config["worker_id"]
        _env = config.get("env_prefix", os.environ.get("ENV_PREFIX", "dev"))
        self.provider_collection = config.get(
            "provider_collection", f"{_env}_PublicHealthData.providers"
        )
        self._states_filter = _build_states_filter(config)  # strict, raises if missing
        self._batch_size = config.get("embed_batch_size", EMBED_BATCH_SIZE)
        self._jitter_max = config.get("embed_initial_jitter", JITTER_MAX_DEFAULT)

        model = config.get("embed_model", EMBED_MODEL)
        if model not in SUPPORTED_EMBED_MODELS:
            raise ValueError(
                f"Unsupported embedding model '{model}'. "
                f"Supported: {sorted(SUPPORTED_EMBED_MODELS)}"
            )
        self._model = model
        self._model_dimensions = SUPPORTED_EMBED_MODELS[model]

        # Throttle pacing — config["throttle"]["openai"]["refill_rate"] is the
        # CLI-supplied tokens/sec for the openai token bucket; we derive a
        # per-batch sleep (1 / refill_rate seconds) so each worker's outbound
        # batch rate matches the configured envelope. The Durable Entity
        # token_bucket/openai is configured with the same refill_rate by the
        # parent orchestrator's preamble.
        from throttle_entities import call_delay_seconds
        self._batch_delay = call_delay_seconds(
            config["throttle"]["openai"]["refill_rate"]
        )


        # State initialised in _pipeline_open(); set to safe defaults so
        # _pipeline_close() is always safe to call even if _pipeline_open()
        # did not complete.
        self._cursor = None
        self._buffer: list = []   # current batch fetched from cursor
        self._batch_num: int = 0
        self._total: int = 0
        self._embedded: int = 0
        self._total_tokens: int = 0
        self._collection = None
        self._oai_client = None

    # ── PipelineWorkerBase overrides ──────────────────────────────────────────

    def _pipeline_open(self) -> None:
        db_name, coll_name = self.provider_collection.split(".", 1)
        self._collection = _get_mongo()[db_name][coll_name]
        self._oai_client = _get_openai()

        # Startup jitter — stagger workers to avoid a synchronised burst at t=0.
        jitter = random.uniform(0, self._jitter_max)
        if jitter > 0.1:
            logging.info("EmbeddingWorker %d: startup jitter %.1f s", self.worker_id, jitter)
            time.sleep(jitter)

        query = {
            "worker_id": self.worker_id,
            "embedding": {"$exists": False},
            **_EMBED_PREFILTER,
            **self._states_filter,
        }
        self._total = self._collection.count_documents(query)
        logging.info("EmbeddingWorker %d: %d docs to embed", self.worker_id, self._total)

        # Server-side cursor — only batch_size docs in memory at a time.
        self._cursor = self._collection.find(
            query, batch_size=self._batch_size, no_cursor_timeout=True
        )
        self._fetch_next_batch()

    def _fetch_next_batch(self) -> None:
        """Pull up to batch_size docs from the cursor into self._buffer."""
        self._buffer = []
        while len(self._buffer) < self._batch_size:
            try:
                doc = next(self._cursor)
            except StopIteration:
                break
            if should_embed(doc):
                self._buffer.append(doc)

    def _pipeline_has_next(self) -> bool:
        return len(self._buffer) > 0

    def _pipeline_process(self) -> None:
        batch = self._buffer
        texts = [render(project(doc)) for doc in batch]

        # Throttle: pace consecutive batch calls per the configured openai
        # bucket refill_rate. Acts BEFORE the API call so the rate ceiling is
        # honoured regardless of how fast the previous batch returned.
        if self._batch_delay > 0:
            time.sleep(self._batch_delay)

        # Retry loop — 429 must not advance the cursor.
        # Only after MAX_RETRIES exhaustion does the exception propagate to
        # PipelineWorkerBase, which then calls _pipeline_resume() to skip the batch.
        response = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self._oai_client.embeddings.create(model=self._model, input=texts)
                break
            except openai.RateLimitError as exc:
                if attempt == MAX_RETRIES - 1:
                    logging.error(
                        "EmbeddingWorker %d: 429 — retry limit reached after %d attempts, skipping batch",
                        self.worker_id, MAX_RETRIES,
                    )
                    raise
                retry_after = _retry_after_seconds(exc)
                delay = retry_after if retry_after else min(
                    RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY
                )
                logging.warning(
                    "EmbeddingWorker %d: 429 rate limit (attempt %d/%d) — sleeping %.1f s",
                    self.worker_id, attempt + 1, MAX_RETRIES, delay,
                )
                time.sleep(delay)

        self._total_tokens += response.usage.total_tokens
        ops = [
            UpdateOne(
                {"_id": doc["_id"]},
                {"$set": {
                    "embedding": data.embedding,
                    "embedding_version": EMBED_VERSION,
                    "embedding_model": self._model,
                }},
            )
            for doc, data in zip(batch, response.data)
        ]
        self._collection.bulk_write(ops, ordered=False)
        self._embedded += len(batch)
        self._batch_num += 1
        self._fetch_next_batch()

    def _pipeline_row_key(self) -> str:
        return f"worker_{self.worker_id}_batch_{self._batch_num}"

    def _pipeline_resume(self) -> None:
        # Skip the failed batch — fetch the next one.
        # Only reached after retry exhaustion (not on 429 alone).
        self._batch_num += 1
        self._fetch_next_batch()

    def _pipeline_build_result(self) -> dict:
        if self._cursor is not None:
            self._cursor.close()
        logging.info(
            "EmbeddingWorker %d: embedded %d documents, %d batches failed, %d tokens used",
            self.worker_id, self._embedded, len(self.row_errors), self._total_tokens,
        )
        return {
            "worker_id": self.worker_id,
            "embedded": self._embedded,
            "failed_batches": len(self.row_errors),
            "batch_errors": self.row_errors,
            "total_tokens": self._total_tokens,
        }
