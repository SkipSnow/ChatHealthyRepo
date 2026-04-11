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


# DEAD CODE (v4-031) -- unreferenced function 'store_decision', marked for deletion
# def store_decision(
#     topic: str,
#     decision: str,
#     rationale: str,
#     risk: str,
#     created_by: str,
#     adr_id: Optional[str] = None,
#     constraints: Optional[list[str]] = None,
#     components: Optional[list[str]] = None,
#     decision_type: str = "architectural",
#     narrative: Optional[str] = None,
# ) -> bool:
#     """
#     Store a decision in Machine Brain. Auto-embeds the record at write time.
#
#     Args:
#         topic:         Short topic name
#         decision:      The decision made
#         rationale:     Why this decision was made
#         risk:          Low | Moderate | High | Critical | Suicidal
#         created_by:    GPT | Claude | Skip
#         adr_id:        ADR/MB reference (e.g. ADR-0007, MB-0001)
#         constraints:   List of constraints this decision imposes
#         components:    List of system components affected
#         decision_type: architectural | operational | security | compliance
#         narrative:     Long-form context explaining the why behind the why
#
#     Returns:
#         True if stored successfully, False otherwise.
#     """
#     valid_risks = {"Low", "Moderate", "High", "Critical", "Suicidal"}
#     if risk not in valid_risks:
#         raise ValueError(f"risk must be one of {valid_risks}")
#
#     record = {
#         "adr_id": adr_id,
#         "topic": topic,
#         "type": decision_type,
#         "decision": decision,
#         "rationale": rationale,
#         "risk": risk,
#         "constraints": constraints or [],
#         "components": components or [],
#         "framework": "framework_02",
#         "created_by": created_by,
#         "created_at": datetime.now(timezone.utc).isoformat(),
#         "version": 1,
#         "embed_model": None,
#         "embedding": None,
#     }
#     if narrative:
#         record["narrative"] = narrative
#
#     # Embed at write time
#     embed_text = _build_embed_text(topic, decision, rationale, constraints, narrative)
#     vec = _embed(embed_text)
#     if vec is not None:
#         record["embedding"] = vec
#         record["embed_model"] = _EMBED_MODEL
#
#     try:
#         col = _get_collection()
#         col.insert_one(record)
#         return True
#     except PyMongoError as e:
#         print(f"[MachineBrain] WARNING: store_decision failed — {e}", flush=True)
#         return False
# END DEAD CODE


# DEAD CODE (v4-031) -- unreferenced function 'backfill_embeddings', marked for deletion
# def backfill_embeddings() -> dict:
#     """
#     Embed all existing records that have embedding=None.
#
#     Safe to run multiple times — skips already-embedded records.
#
#     Returns:
#         {"embedded": int, "skipped": int, "failed": int}
#     """
#     vc = _get_voyage()
#     if vc is None:
#         raise EnvironmentError("VOYAGE_API_KEY is required for backfill_embeddings().")
#
#     col = _get_collection()
#     records = list(col.find({"embedding": None}, {"_id": 1, "topic": 1, "decision": 1,
#                                                     "rationale": 1, "constraints": 1,
#                                                     "narrative": 1}))
#     counts = {"embedded": 0, "skipped": 0, "failed": 0}
#     for r in records:
#         text = _build_embed_text(
#             r.get("topic", ""),
#             r.get("decision", ""),
#             r.get("rationale", ""),
#             r.get("constraints"),
#             r.get("narrative"),
#         )
#         vec = _embed(text)
#         if vec is None:
#             counts["failed"] += 1
#             continue
#         try:
#             col.update_one(
#                 {"_id": r["_id"]},
#                 {"$set": {"embedding": vec, "embed_model": _EMBED_MODEL}},
#             )
#             counts["embedded"] += 1
#         except PyMongoError as e:
#             print(f"[MachineBrain] WARNING: backfill update failed — {e}", flush=True)
#             counts["failed"] += 1
#
#     print(f"[MachineBrain] Backfill complete: {counts}", flush=True)
#     return counts
# END DEAD CODE


# DEAD CODE (v4-031) -- unreferenced function 'list_all_decisions', marked for deletion
# def list_all_decisions() -> list[dict]:
#     """Return all decisions (no embeddings), sorted by created_at descending."""
#     try:
#         col = _get_collection()
#         return list(col.find({}, {"_id": 0, "embedding": 0}).sort("created_at", -1))
#     except PyMongoError as e:
#         print(f"[MachineBrain] WARNING: list_all_decisions failed — {e}", flush=True)
#         return []
# END DEAD CODE
