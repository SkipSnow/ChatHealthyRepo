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
import os
from typing import Optional

_log = logging.getLogger("findcare.specialty")


_NORMALIZE_SYSTEM_PROMPT = (
    "You translate a user's natural-language healthcare request into "
    "medically-appropriate specialty terminology suitable for matching "
    "against the NUCC taxonomy via embedding similarity. "
    "Output ONE short phrase, no explanation, no punctuation other than "
    "spaces. Examples: 'shrink' -> 'mental health provider'; "
    "'kid's doctor' -> 'pediatrician'; 'orthopedist' -> 'orthopedic surgery'; "
    "'eye doctor' -> 'ophthalmology'; 'I need a gynecologist in VA' -> "
    "'obstetrics and gynecology'."
)


class SpecialtyService:
    """Specialty identification: AI normalization + AI vector search.

    Two-step pipeline:
      Step 1 — LLM normalizes the user's prompt to medically-appropriate
               specialty terminology (translates lay/colloquial language
               into NUCC-aligned terms).
      Step 2 — embed the normalized text with the canonical embedding model
               (see EPIC-008-F-011-S-004-REQ-B-001) and vector-search
               SpecialtyMetaData via cosine similarity. Results sorted in
               descending similarity order.
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

    def _normalize_query(self, query: str) -> str:
        """LLM normalization step using strict Pydantic input + output.

        Input model:  SpecialtyNormalizeInput  (validates the user's query)
        Output model: SpecialtyNormalizeOutput (parsed via OpenAI .parse())

        On any failure, returns the original query unchanged (degraded
        behavior, not fatal — vector search still runs on raw text).
        """
        from application.tool_models.specialty_normalize_models import (
            SpecialtyNormalizeInput,
            SpecialtyNormalizeOutput,
        )

        try:
            payload = SpecialtyNormalizeInput(query=query)
        except Exception as exc:
            _log.warning("normalize input invalid (%s); using raw query", exc)
            return query

        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            _log.warning("normalize: OPENAI_API_KEY missing; using raw query")
            return query
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            resp = client.beta.chat.completions.parse(
                model="gpt-4.1-mini",
                max_tokens=40,
                messages=[
                    {"role": "system", "content": _NORMALIZE_SYSTEM_PROMPT},
                    {"role": "user", "content": payload.query},
                ],
                response_format=SpecialtyNormalizeOutput,
            )
            parsed: SpecialtyNormalizeOutput = resp.choices[0].message.parsed
            normalized = (parsed.normalized_text or "").strip() if parsed else ""
            if not normalized:
                return query
            _log.info("normalize: %r -> %r", query, normalized)
            return normalized
        except Exception as exc:
            _log.warning("normalize failed (%s); using raw query", exc)
            return query

    def find_specialty_codes(self, query: str, chat_history: Optional[list[str]] = None) -> dict:
        """Find NUCC specialty codes matching a query via two-step pipeline.

        Step 1: LLM normalization (medically-appropriate terminology).
        Step 2: vector search against SpecialtyMetaData embeddings.

        Returns:
            dict with "specialties" list, each containing:
                Code, Display Name, can_prescribe, homeopathic, rank
        """
        db = self._get_db()
        if db is None:
            return {"error": "Database unavailable"}

        # Build query text from chat history + current query
        query_parts = []
        if chat_history:
            query_parts.extend(chat_history[-5:])
        query_parts.append(query)
        raw_query_text = " ".join(query_parts)

        # Step 1: LLM normalization
        normalized_text = self._normalize_query(raw_query_text)

        # Step 2: embed the NORMALIZED text
        query_vector = self._get_vector(normalized_text)
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
                    "numCandidates": 883,
                    "limit": 698,
                    "filter": {"Section": "Individual"},
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
