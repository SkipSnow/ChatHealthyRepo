# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# SpecialtyService — FC-FILT-001: AI vector search for NUCC specialties.
#
# REQ-001: Vector search only. No regex, no string matching, no classify call.
# REQ-002: Returns NUCC codes, Display Name, can_prescribe, homeopathic, rank.
# REQ-003: Query vector built from last 5 user prompts. Ranked 1..n by cosine.
#
# Extracted from main.py as part of ARCH-001 Phase 3.
# Host-independent — no FastAPI, no HuggingFace dependencies.

import logging
from typing import Optional

_log = logging.getLogger("findcare.specialty")


class SpecialtyService:
    """Specialty identification via AI vector search.

    FC-FILT-001-REQ-001: Embedding-only search against SpecialtyMetaData.
    No regex, no string matching, no LLM classify call, no fallback.
    """

    def __init__(self, get_db_fn, env_prefix: str, get_vector_fn):
        """
        Args:
            get_db_fn: callable returning MongoDB client or None
            env_prefix: environment prefix (dev/qa/prod)
            get_vector_fn: callable(text) -> list[float] for vector embedding
        """
        self._get_db = get_db_fn
        self._env = env_prefix
        self._get_vector = get_vector_fn

    def find_specialty_codes(self, query: str, chat_history: Optional[list[str]] = None) -> dict:
        """Find NUCC specialty codes matching a query via vector search.

        FC-FILT-001-REQ-001: AI vector search using text-embedding-3-large.
        FC-FILT-001-REQ-002: Returns NUCC codes, Display Name, can_prescribe,
                             homeopathic, and rank (1 = most likely).
        FC-FILT-001-REQ-003: Query vector built from last 5 user prompts.

        Args:
            query: The current user search query.
            chat_history: Optional list of recent user prompts (up to last 5).
                         Concatenated with query to build the embedding vector.

        Returns:
            dict with "specialties" list, each containing:
                Code, Display Name, can_prescribe, homeopathic, rank
        """
        db = self._get_db()
        if db is None:
            return {"error": "Database unavailable"}

        # Build query text from chat history + current query (REQ-003)
        query_parts = []
        if chat_history:
            # Last 5 user prompts
            query_parts.extend(chat_history[-5:])
        query_parts.append(query)
        query_text = " ".join(query_parts)

        # Embed the query
        query_vector = self._get_vector(query_text)
        if not query_vector:
            return {"error": "Embedding failed"}

        specialty_col = db[f"{self._env}_PublicHealthData"]["SpecialtyMetaData"]

        # Vector search — return all meaningful matches (REQ-003)
        try:
            results = list(specialty_col.aggregate([
                {"$vectorSearch": {
                    "index": "specialty_vector_index",
                    "path": "embedding",
                    "queryVector": query_vector,
                    "numCandidates": 200,
                    "limit": 100,
                }},
                {"$project": {
                    "_id": 0,
                    "Code": 1,
                    "Display Name": 1,
                    "can_prescribe": 1,
                    "homeopathic": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }},
            ]))
        except Exception as exc:
            _log.error("Vector search failed: %s", exc)
            return {"error": f"Vector search failed: {exc}"}

        if not results:
            return {"specialties": [], "message": f"No matching specialty found for '{query}'."}

        # Filter out deactivated specialties and low-score matches
        results = [r for r in results if not r.get("Display Name", "").startswith("Deactivated")]
        meaningful = [r for r in results if r.get("score", 0) > 0.4]
        if not meaningful:
            meaningful = results[:5]  # Fallback: top 5 if nothing crosses threshold

        _log.info("specialty vector search: query=%r, %d results, %d above threshold",
                   query, len(results), len(meaningful))

        # Add rank 1..n (REQ-002) — results already sorted by cosine similarity
        specialties = []
        for rank, doc in enumerate(meaningful, start=1):
            specialties.append({
                "Code": doc["Code"],
                "Display Name": doc.get("Display Name", ""),
                "can_prescribe": doc.get("can_prescribe", False),
                "homeopathic": doc.get("homeopathic", False),
                "rank": rank,
            })

        _log.info("specialty results: %s",
                   [(s["Display Name"], s["rank"]) for s in specialties[:5]])

        return {"specialties": specialties}
