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


class EmbeddingWorker:

    def __init__(self, config: dict):
        self.worker_id = config["worker_id"]
        self.staging_collection = config.get(
            "staging_collection", "PublicHealthData.providers_staging"
        )

    def run(self) -> dict:
        db_name, coll_name = self.staging_collection.split(".", 1)
        collection = _get_mongo()[db_name][coll_name]
        oai = _get_openai()

        # Only process documents not yet embedded — idempotent on retry.
        # Embed if: never deactivated, OR deactivated and later reactivated (currently active).
        # Purely deactivated records (no reactivation date) are retained but not embedded.
        docs = list(collection.find({
            "worker_id": self.worker_id,
            "embedding": {"$exists": False},
            "$or": [
                {"npi_deactivation_date": {"$exists": False}},
                {"npi_reactivation_date": {"$exists": True}},
            ],
        }))
        total = len(docs)
        embedded = 0

        for i in range(0, total, EMBED_BATCH_SIZE):
            batch = docs[i:i + EMBED_BATCH_SIZE]
            texts = [_build_embed_text(doc) for doc in batch]
            response = oai.embeddings.create(model=EMBED_MODEL, input=texts)
            ops = [
                UpdateOne(
                    {"_id": doc["_id"]},
                    {"$set": {"embedding": data.embedding}},
                )
                for doc, data in zip(batch, response.data)
            ]
            collection.bulk_write(ops, ordered=False)
            embedded += len(batch)

        logging.info(
            "EmbeddingWorker %d: embedded %d active documents", self.worker_id, embedded
        )
        return {"worker_id": self.worker_id, "embedded": embedded}
