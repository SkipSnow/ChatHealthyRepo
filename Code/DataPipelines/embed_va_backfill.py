# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Backfill embeddings for VA providers missing them.
# Uses the canonical provider_embedding pipeline (should_embed, project, render).
# Run: python embed_va_backfill.py

import logging
import os
import sys
import time

import openai
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

sys.path.insert(0, os.path.dirname(__file__))
from provider_embedding import should_embed, project, render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("embed_va_backfill")

EMBED_MODEL = "text-embedding-3-large"
BATCH_SIZE = 100
MAX_RETRIES = 5
RETRY_BASE_DELAY = 10.0


def main():
    STATE = sys.argv[1] if len(sys.argv) > 1 else "VA"
    log.info("Backfilling embeddings for state: %s", STATE)
    mongo = MongoClient(os.environ["MONGO_connectionString"], serverSelectionTimeoutMS=60000)
    oai = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    col = mongo[f"{os.environ.get('ENV_PREFIX', 'dev')}_PublicHealthData"]["providers"]

    # Count missing
    query = {
        "practice_address.state": STATE,
        "embedding": {"$exists": False},
        "county.reason": {"$nin": ["no_address", "zip_state_mismatch"]},
    }
    total_missing = col.count_documents(query)
    log.info("VA providers missing embeddings: %d", total_missing)

    if total_missing == 0:
        log.info("Nothing to do.")
        return

    cursor = col.find(query).batch_size(BATCH_SIZE)
    embedded = 0
    skipped = 0
    failed_batches = 0
    start_time = time.time()

    batch_texts = []
    batch_ids = []

    for doc in cursor:
        if not should_embed(doc):
            skipped += 1
            continue

        proj = project(doc)
        text = render(proj)
        batch_texts.append(text)
        batch_ids.append(doc["_id"])

        if len(batch_texts) >= BATCH_SIZE:
            success = _embed_batch(oai, col, batch_texts, batch_ids)
            if success:
                embedded += len(batch_texts)
            else:
                failed_batches += 1
            batch_texts = []
            batch_ids = []

            if embedded % 1000 == 0 and embedded > 0:
                elapsed = time.time() - start_time
                rate = embedded / elapsed * 3600
                remaining = (total_missing - embedded - skipped) / (rate / 3600) if rate > 0 else 0
                log.info(
                    "Progress: %d/%d embedded, %d skipped, %d failed batches, %.0f/hr, ~%.0f min remaining",
                    embedded, total_missing, skipped, failed_batches, rate, remaining / 60,
                )

    # Final batch
    if batch_texts:
        success = _embed_batch(oai, col, batch_texts, batch_ids)
        if success:
            embedded += len(batch_texts)
        else:
            failed_batches += 1

    elapsed = time.time() - start_time
    log.info(
        "COMPLETE: %d embedded, %d skipped, %d failed batches in %.1f minutes",
        embedded, skipped, failed_batches, elapsed / 60,
    )


def _embed_batch(oai, col, texts, ids):
    """Embed a batch of texts and write back to MongoDB. Returns True on success."""
    for attempt in range(MAX_RETRIES):
        try:
            response = oai.embeddings.create(model=EMBED_MODEL, input=texts)
            updates = []
            for i, emb_data in enumerate(response.data):
                updates.append(UpdateOne(
                    {"_id": ids[i]},
                    {"$set": {"embedding": emb_data.embedding}},
                ))
            if updates:
                col.bulk_write(updates, ordered=False)
            return True

        except openai.RateLimitError as e:
            delay = min(RETRY_BASE_DELAY * (2 ** attempt), 120)
            log.warning("Rate limited (attempt %d/%d), retrying in %.0fs: %s", attempt + 1, MAX_RETRIES, delay, e)
            time.sleep(delay)

        except Exception as e:
            log.error("Embedding batch failed: %s", e)
            return False

    log.error("Batch failed after %d retries, skipping", MAX_RETRIES)
    return False


if __name__ == "__main__":
    main()
