# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Provider embedding engine — LLD v23 §3.2 (embeddings step) + F-102-S-006.

Realizes:

  - EPIC-010-F-102-S-006-REQ-B-001..REQ-B-005  shared embedding worker;
                                               text-embedding-3-large;
                                               write vector back to record
  - EPIC-010-F-103-S-006-REQ-B-001, REQ-B-002  provider embeddings
  - EPIC-008-F-011-S-004-REQ-B-001             canonical embedding model
                                               is text-embedding-3-large

Contract:
  Embedding model:    text-embedding-3-large (OpenAI)
  Vector dimensions:  3072 (native for the canonical model)
  Vector field:       providers_v<N>.embedding
  Version field:      providers_v<N>.embedding_model
  Timestamp:          providers_v<N>.embedding_generated_at

Input text is the concatenation of the semantic fields on the provider
record: display name, primary taxonomy label, secondary taxonomy labels,
practice-address cities and states. The concatenation shape is what the
front-end vector search consumes; it lives here rather than on the record
builder because the embedding step is where the vector-space semantics are
declared.

Idempotency: a provider record whose `embedding_model` already equals the
target model and whose `embedding` is 3072-dimensional is skipped. The
step is safe to re-run.

Concurrency: OpenAI batching — up to `batch_size` records per API call.
Bounded thread pool for parallelism across batches.

Public entry point: `generate_provider_embeddings(config, mongo, blob)`.
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException


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


def _compose_provider_text(doc: dict) -> str:
    """Concatenation of semantic fields for the provider vector search."""
    parts: list[str] = []
    name = doc.get("display_name") or doc.get("name") or ""
    if name:
        parts.append(str(name))
    for tax in (doc.get("taxonomies") or []):
        if isinstance(tax, dict):
            lbl = tax.get("label") or tax.get("desc") or ""
            if lbl:
                parts.append(str(lbl))
    for addr in (doc.get("addresses") or []):
        if isinstance(addr, dict) and addr.get("address_type", "").endswith("practice"):
            city = addr.get("city") or ""
            state = addr.get("state") or ""
            county = ((addr.get("county") or {}).get("name") if isinstance(addr.get("county"), dict) else None) or ""
            joined = " ".join(p for p in (city, county, state) if p)
            if joined:
                parts.append(joined)
    return " | ".join(parts).strip()


def _build_openai_client(api_key: str | None):
    key = api_key or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise ChatHealthyException(mode="runtime_error", message="embedding_engine: OPENAI_API_KEY is not set")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ChatHealthyException(mode="runtime_error", message="embedding_engine: openai package not installed; add to requirements") from exc
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


def generate_provider_embeddings(
    config: dict,
    *,
    mongo=None,
    blob=None,
) -> dict[str, Any]:
    """Generate text-embedding-3-large embeddings for every provider needing one.

    Required config keys:
      - run_id                (str)
      - env                   (str)
      - provider_collection   (str "<db>.<coll>")
      - batch_size            (int, default 96)
      - max_in_flight         (int, default 4)
      - openai_api_key        (str; falls back to OPENAI_API_KEY env)
      - partition_state       (str | None) — restrict scan to this state

    Returns:
      {
        "candidate_count": int,
        "updated_count":   int,
        "failed_count":    int,
        "model":           str,
        "dimensions":      int,
      }
    """
    run_id = config["run_id"]
    provider_collection = config["provider_collection"]
    batch_size = int(config.get("batch_size", DEFAULT_BATCH_SIZE))
    max_in_flight = int(config.get("max_in_flight", DEFAULT_MAX_IN_FLIGHT))
    partition_state = (config.get("partition_state") or "").upper() or None

    db_name, coll_name = provider_collection.split(".", 1)
    coll = mongo[db_name][coll_name]

    # NPI-atomic ownership: partition by BUSINESS mailing address state.
    # Business address is single-valued per NPPES contract, so $elemMatch
    # gives exactly one owner per NPI. Practice addresses are optional
    # and multi-valued (secondary practices) so cannot serve as key.
    query: dict[str, Any] = {"run_id": run_id}
    if partition_state:
        query["addresses"] = {"$elemMatch": {"address_type": "business", "state": partition_state}}

    client = _build_openai_client(config.get("openai_api_key"))

    batches: list[list[tuple[Any, str]]] = []
    current: list[tuple[Any, str]] = []
    candidate = 0

    for doc in coll.find(query):
        if not _needs_embedding(doc):
            continue
        text = _compose_provider_text(doc)
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
