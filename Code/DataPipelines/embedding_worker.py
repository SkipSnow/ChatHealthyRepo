# Copyright © 2026 Skip Snow. All rights reserved.
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
from county_enrichment_job import _build_states_query
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
        self.staging_collection = config.get(
            "staging_collection", "PublicHealthData.providers_staging"
        )
        self._states_query = _build_states_query(config)  # {} if no filter
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


        # State initialised in _pipeline_open(); set to safe defaults so
        # _pipeline_close() is always safe to call even if _pipeline_open()
        # did not complete.
        self._docs: list = []
        self._total: int = 0
        self._batch_idx: int = 0
        self._embedded: int = 0
        self._total_tokens: int = 0
        self._collection = None
        self._oai_client = None

    # ── PipelineWorkerBase overrides ──────────────────────────────────────────

    def _pipeline_open(self) -> None:
        db_name, coll_name = self.staging_collection.split(".", 1)
        self._collection = _get_mongo()[db_name][coll_name]
        self._oai_client = _get_openai()

        # Startup jitter — stagger workers to avoid a synchronised burst at t=0.
        jitter = random.uniform(0, self._jitter_max)
        if jitter > 0.1:
            logging.info("EmbeddingWorker %d: startup jitter %.1f s", self.worker_id, jitter)
            time.sleep(jitter)

        self._docs = list(self._collection.find({
            "worker_id": self.worker_id,
            "embedding": {"$exists": False},
            **_EMBED_PREFILTER,
            **self._states_query,
        }))
        # Apply authoritative should_embed() filter after load
        self._docs = [d for d in self._docs if should_embed(d)]
        self._total = len(self._docs)

    def _pipeline_has_next(self) -> bool:
        return self._batch_idx < self._total

    def _pipeline_process(self) -> None:
        end = min(self._batch_idx + self._batch_size, self._total)
        batch = self._docs[self._batch_idx:end]
        texts = [render(project(doc)) for doc in batch]

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
        self._batch_idx = end

    def _pipeline_row_key(self) -> str:
        batch_num = self._batch_idx // self._batch_size
        return f"worker_{self.worker_id}_batch_{batch_num}"

    def _pipeline_resume(self) -> None:
        # Skip the failed batch — advance the cursor to the next batch.
        # Only reached after retry exhaustion (not on 429 alone).
        self._batch_idx = min(self._batch_idx + self._batch_size, self._total)

    def _pipeline_build_result(self) -> dict:
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
