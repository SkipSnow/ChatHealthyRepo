# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Specialty embedding engine — LLD v23 §3.2 (embeddings step) + F-102-S-006.

Realizes:

  - EPIC-010-F-102-S-006-REQ-B-001..REQ-B-005  shared embedding worker;
                                               text-embedding-3-large;
                                               write vector back to record
  - EPIC-008-F-011-S-004-REQ-B-001             canonical embedding model
                                               is text-embedding-3-large

Contract:
  Embedding model:    text-embedding-3-large (OpenAI)
  Vector dimensions:  3072 (native for the canonical model)
  Vector field:       SpecialtyMetaData_v<N>.embedding
  Version field:      SpecialtyMetaData_v<N>.embedding_model
  Timestamp:          SpecialtyMetaData_v<N>.embedding_generated_at

Provider records are NOT embedded. The pipeline embeds the specialty
catalog and nothing else; a provider is found by filtering on the specialty
codes the catalog resolves, not by a vector on the provider itself.

Idempotency: a record whose `embedding_model` already equals the target
model and whose `embedding` is 3072-dimensional is skipped. The step is
safe to re-run.

Concurrency: OpenAI batching — up to `batch_size` records per API call.
Bounded thread pool for parallelism across batches.

Public entry point: `generate_specialty_embeddings(config, mongo, blob)`.
"""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException


import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from pymongo import UpdateOne

_log = ChatHealthyLoggingService()

CANONICAL_MODEL = "text-embedding-3-large"
CANONICAL_DIM = 3072
DEFAULT_BATCH_SIZE = 96
DEFAULT_MAX_IN_FLIGHT = 4


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _needs_embedding(doc: dict) -> bool:
    if doc.get("embedding_model") != CANONICAL_MODEL:
        return True
    vec = doc.get("embedding")
    if not isinstance(vec, list) or len(vec) != CANONICAL_DIM:
        return True
    return False


def _build_openai_client(api_key: str | None):
    key = api_key or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise ChatHealthyException(mode="runtime_error", message="embedding_engine: OPENAI_API_KEY is not set")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ChatHealthyException(mode="runtime_error", message="embedding_engine: openai package not installed; add to requirements",
            exception=exc) from exc
    return OpenAI(api_key=key)


def _embed_batch(client, texts: list[str]) -> list[list[float]]:
    resp = client.embeddings.create(model=CANONICAL_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def _process_batch(
    *,
    client,
    coll,
    batch: list[tuple[Any, str]],
) -> tuple[int, int]:
    """Embed one batch, return (updated, failed)."""
    if not batch:
        return 0, 0
    texts = [text for _id, text in batch]
    try:
        vectors = _embed_batch(client, texts)
    except Exception as exc:
        _log.warning("embedding_engine: batch of %d failed: %s", len(batch), exc)
        return 0, len(batch)
    ops: list[UpdateOne] = []
    now = _now_iso()
    for (doc_id, _text), vec in zip(batch, vectors):
        ops.append(UpdateOne(
            {"_id": doc_id},
            {"$set": {
                "embedding": vec,
                "embedding_model": CANONICAL_MODEL,
                "embedding_generated_at": now,
            }},
        ))
    if ops:
        result = coll.bulk_write(ops, ordered=False)
        return (result.modified_count or 0), 0
    return 0, 0


def _compose_specialty_text(doc: dict) -> str:
    """Concatenation of semantic fields for the SpecialtyMetaData vector search.

    v42 §5.2.8a: the SMD collection ($vectorSearch target for FindCare
    specialty filter) needs one embedding per NUCC/F-105 code. Composition
    mirrors the display fields a human sees when reading a specialty page:
    Display Name, Grouping, Classification, Specialization, Definition.
    Empty fields are skipped so the composed string never carries `|| ||`
    filler that would poison the embedding.
    """
    parts: list[str] = []
    for key in ("Display Name", "Grouping", "Classification", "Specialization", "Definition"):
        val = doc.get(key)
        if val:
            s = str(val).strip()
            if s:
                parts.append(s)
    return " | ".join(parts).strip()


def generate_specialty_embeddings(
    config: dict,
    *,
    mongo=None,
) -> dict[str, Any]:
    """Generate text-embedding-3-large embeddings for every SMD row needing one.

    Required config keys:
      - specialty_collection  (str "<db>.<coll>")
      - batch_size            (int, default 96)
      - max_in_flight         (int, default 4)
      - openai_api_key        (str; falls back to OPENAI_API_KEY env)

    Iterates every doc in the target collection (no run_id filter — SMD
    is a full-replace snapshot per publish, not a running-accumulation).
    Returns candidate/updated/failed counts + model + dimensions.
    """
    specialty_collection = config["specialty_collection"]
    batch_size = int(config.get("batch_size", DEFAULT_BATCH_SIZE))
    max_in_flight = int(config.get("max_in_flight", DEFAULT_MAX_IN_FLIGHT))

    db_name, coll_name = specialty_collection.split(".", 1)
    coll = mongo[db_name][coll_name]

    client = _build_openai_client(config.get("openai_api_key"))

    batches: list[list[tuple[Any, str]]] = []
    current: list[tuple[Any, str]] = []
    candidate = 0

    for doc in coll.find({}):
        if not _needs_embedding(doc):
            continue
        text = _compose_specialty_text(doc)
        if not text:
            continue
        candidate += 1
        current.append((doc["_id"], text))
        if len(current) >= batch_size:
            batches.append(current)
            current = []
    if current:
        batches.append(current)

    updated = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, max_in_flight)) as pool:
        futs = [pool.submit(_process_batch, client=client, coll=coll, batch=b) for b in batches]
        for fut in as_completed(futs):
            u, f = fut.result()
            updated += u
            failed += f

    return {
        "candidate_count": candidate,
        "updated_count": updated,
        "failed_count": failed,
        "model": CANONICAL_MODEL,
        "dimensions": CANONICAL_DIM,
    }
