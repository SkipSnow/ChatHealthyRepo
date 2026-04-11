# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""
MachineBrain — Persistent architectural memory for ChatHealthy.

Embedding model: voyage-3-large (Voyage AI, 1024 dimensions)
  - store_decision() auto-embeds every record at write time
  - semantic_search(query) uses Atlas Vector Search (cosine similarity)
  - get_decisions(topic) provides keyword fallback when VOYAGE_API_KEY is absent
  - backfill_embeddings() backfills existing un-embedded records

framework_02 enforcement:
  - Query before implementing: semantic_search(query)  [preferred]
                                get_decisions(topic)   [keyword fallback]
  - Write after deciding:       store_decision(...)

Required env vars:
  MONGODB_CONNECTION_STRING   — ChatHealthyFrontEndCluster connection string
  VOYAGE_API_KEY              — Voyage AI API key (embeddings disabled if absent)
  ENV_PREFIX                  — dev | qa | prod  (default: dev)

Database: {ENV_PREFIX}_MachineBrain on ChatHealthyFrontEndCluster
Collection: decisions
Vector index: decisions_vector_index

ADR: ADR-0007
"""

import os
from datetime import datetime, timezone
from typing import Optional
from pymongo import MongoClient, ASCENDING
from pymongo.errors import PyMongoError

ENV_PREFIX = os.getenv("ENV_PREFIX", "dev")
_DB_NAME = f"{ENV_PREFIX}_MachineBrain"
_COLLECTION = "decisions"
_VECTOR_INDEX = "decisions_vector_index"
_EMBED_MODEL = "voyage-3-large"
_EMBED_DIMS = 1024

_client: Optional[MongoClient] = None
_voyage = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_collection():
    global _client
    if _client is None:
        conn = os.getenv("MONGODB_CONNECTION_STRING")
        if not conn:
            raise EnvironmentError("MONGODB_CONNECTION_STRING is not set.")
        _client = MongoClient(conn)
        _client.admin.command("ping")
        col = _client[_DB_NAME][_COLLECTION]
        col.create_index([("topic", ASCENDING)])
        col.create_index([("adr_id", ASCENDING)])
        col.create_index([("components", ASCENDING)])
        _ensure_vector_index(col)
    return _client[_DB_NAME][_COLLECTION]


def _ensure_vector_index(col):
    """Create Atlas Vector Search index if it does not already exist."""
    try:
        existing = {idx["name"] for idx in col.list_search_indexes()}
        if _VECTOR_INDEX not in existing:
            col.create_search_index({
                "name": _VECTOR_INDEX,
                "type": "vectorSearch",
                "definition": {
                    "fields": [
                        {
                            "type": "vector",
                            "path": "embedding",
                            "numDimensions": _EMBED_DIMS,
                            "similarity": "cosine",
                        }
                    ]
                },
            })
            print(f"[MachineBrain] Atlas Vector Search index '{_VECTOR_INDEX}' created.", flush=True)
    except Exception as e:
        # Index creation is non-fatal — keyword search still works
        print(f"[MachineBrain] WARNING: vector index setup failed — {e}", flush=True)


def _get_voyage():
    """Lazy-init Voyage AI client. Returns None if API key absent."""
    global _voyage
    if _voyage is None:
        key = os.getenv("VOYAGE_API_KEY")
        if not key:
            return None
        import voyageai
        _voyage = voyageai.Client(api_key=key)
    return _voyage


def _build_embed_text(
    topic: str,
    decision: str,
    rationale: str,
    constraints: Optional[list[str]] = None,
    narrative: Optional[str] = None,
) -> str:
    """
    Concatenate decision fields into a single string for embedding.
    Field order is stable so re-embeds produce consistent vectors.
    """
    parts = [
        f"topic: {topic}",
        f"decision: {decision}",
        f"rationale: {rationale}",
    ]
    if constraints:
        parts.append("constraints: " + " | ".join(constraints))
    if narrative:
        parts.append(f"narrative: {narrative}")
    return "\n".join(parts)


def _embed(text: str) -> Optional[list[float]]:
    """
    Generate a voyage-3-large embedding vector for the given text.
    Returns None if VOYAGE_API_KEY is not set or call fails.
    """
    vc = _get_voyage()
    if vc is None:
        return None
    try:
        result = vc.embed([text], model=_EMBED_MODEL, input_type="document")
        return result.embeddings[0]
    except Exception as e:
        print(f"[MachineBrain] WARNING: embedding failed — {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def semantic_search(query: str, top_k: int = 5) -> list[dict]:
    """
    Search Machine Brain using semantic similarity (Atlas Vector Search).

    Preferred query method. Understands meaning, not just keywords.
    Falls back to get_decisions() if embedding is unavailable.

    Args:
        query:  Natural language question or topic description
        top_k:  Maximum number of results to return (default 5)

    Returns:
        List of matching decision records, highest similarity first.
        Each record includes a 'score' field (cosine similarity, 0–1).
    """
    query_vec = _embed(query)
    if query_vec is None:
        print("[MachineBrain] Voyage API unavailable — falling back to keyword search.", flush=True)
        return get_decisions(query)

    try:
        col = _get_collection()
        pipeline = [
            {
                "$vectorSearch": {
                    "index": _VECTOR_INDEX,
                    "path": "embedding",
                    "queryVector": query_vec,
                    "numCandidates": top_k * 10,
                    "limit": top_k,
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "embedding": 0,
                    "score": {"$meta": "vectorSearchScore"},
                }
            },
        ]
        return list(col.aggregate(pipeline))
    except PyMongoError as e:
        print(f"[MachineBrain] WARNING: semantic_search failed — {e}", flush=True)
        return get_decisions(query)


def get_decisions(topic: str) -> list[dict]:
    """
    Keyword search across topic, components, and decision fields.

    Use semantic_search() when possible. Use this for exact ID lookups
    (e.g. get_decisions("ADR-0007")) or when VOYAGE_API_KEY is absent.

    Args:
        topic: keyword, phrase, or ADR/MB ID to search for

    Returns:
        List of matching decision records, most recent first.
    """
    try:
        col = _get_collection()
        return list(
            col.find(
                {"$or": [
                    {"topic": {"$regex": topic, "$options": "i"}},
                    {"adr_id": {"$regex": topic, "$options": "i"}},
                    {"components": {"$regex": topic, "$options": "i"}},
                    {"decision": {"$regex": topic, "$options": "i"}},
                ]},
                {"_id": 0, "embedding": 0},
            ).sort("created_at", -1)
        )
    except PyMongoError as e:
        print(f"[MachineBrain] WARNING: get_decisions failed — {e}", flush=True)
        return []


