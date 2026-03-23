# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""EmbeddingWorker — generates OpenAI text-embedding-3-small embeddings for all
provider documents assigned to a given worker_id and stores them back to the
staging collection.

Idempotent: skips documents that already have an 'embedding' field.
"""

import logging
import os

import openai
from pipeline_worker_base import PipelineWorkerBase
from pymongo import MongoClient, UpdateOne

_mongo: MongoClient | None = None
_oai: openai.OpenAI | None = None

EMBED_MODEL = "text-embedding-3-small"   # 1536 dimensions, $0.02/1M tokens
EMBED_BATCH_SIZE = 500                   # OpenAI allows up to 2048; 500 is conservative

# Internal fields — no semantic value, excluded from embed text
EMBED_EXCLUDE = {"_id", "record_id", "load_id", "worker_id", "county", "embedding"}


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


def _collect_text(value, parts: list) -> None:
    """Recursively collect all non-empty string values from a document field."""
    if isinstance(value, str):
        v = value.strip()
        if v:
            parts.append(v)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_text(v, parts)
    elif isinstance(value, list):
        for item in value:
            _collect_text(item, parts)
    # bool, int, float, None — skip


def _build_embed_text(doc: dict) -> str:
    """Serialize all meaningful document fields into a single text string for embedding."""
    parts = []
    for key, value in doc.items():
        if key in EMBED_EXCLUDE:
            continue
        _collect_text(value, parts)
    return " ".join(parts)


class EmbeddingWorker(PipelineWorkerBase):

    def __init__(self, config: dict):
        super().__init__(config)
        self.worker_id = config["worker_id"]
        self.staging_collection = config.get(
            "staging_collection", "PublicHealthData.providers_staging"
        )

        # State initialised in _pipeline_open(); set to safe defaults so
        # _pipeline_close() is always safe to call even if _pipeline_open()
        # did not complete.
        self._docs: list = []
        self._total: int = 0
        self._batch_idx: int = 0
        self._embedded: int = 0
        self._collection = None
        self._oai_client = None

    # ── PipelineWorkerBase overrides ──────────────────────────────────────────

    def _pipeline_open(self) -> None:
        db_name, coll_name = self.staging_collection.split(".", 1)
        self._collection = _get_mongo()[db_name][coll_name]
        self._oai_client = _get_openai()

        # Load all unembedded documents for this worker upfront.
        # Embed if: never deactivated, OR deactivated and later reactivated (currently active).
        # Purely deactivated records (no reactivation date) are retained but not embedded.
        self._docs = list(self._collection.find({
            "worker_id": self.worker_id,
            "embedding": {"$exists": False},
            "$or": [
                {"npi_deactivation_date": {"$exists": False}},
                {"npi_reactivation_date": {"$exists": True}},
            ],
        }))
        self._total = len(self._docs)

    def _pipeline_has_next(self) -> bool:
        return self._batch_idx < self._total

    def _pipeline_process(self) -> None:
        end = min(self._batch_idx + EMBED_BATCH_SIZE, self._total)
        batch = self._docs[self._batch_idx:end]
        texts = [_build_embed_text(doc) for doc in batch]
        response = self._oai_client.embeddings.create(model=EMBED_MODEL, input=texts)
        ops = [
            UpdateOne(
                {"_id": doc["_id"]},
                {"$set": {"embedding": data.embedding}},
            )
            for doc, data in zip(batch, response.data)
        ]
        self._collection.bulk_write(ops, ordered=False)
        self._embedded += len(batch)
        self._batch_idx = end

    def _pipeline_row_key(self) -> str:
        batch_num = self._batch_idx // EMBED_BATCH_SIZE
        return f"worker_{self.worker_id}_batch_{batch_num}"

    def _pipeline_resume(self) -> None:
        # Skip the failed batch — advance the cursor to the next batch.
        self._batch_idx = min(self._batch_idx + EMBED_BATCH_SIZE, self._total)

    def _pipeline_build_result(self) -> dict:
        logging.info(
            "EmbeddingWorker %d: embedded %d documents, %d batches failed",
            self.worker_id, self._embedded, len(self.row_errors),
        )
        return {
            "worker_id": self.worker_id,
            "embedded": self._embedded,
            "failed_batches": len(self.row_errors),
            "batch_errors": self.row_errors,
        }
