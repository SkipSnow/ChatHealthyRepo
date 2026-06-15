# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# EmbeddingClient — centralized AI embedding and query expansion.
# Source: OpenAI (embeddings), Anthropic Haiku (query expansion)

import json
from chathealthy_frontend_lib import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException
import os
from typing import Optional

from anthropic import Anthropic
from openai import OpenAI

log = ChatHealthyLoggingService()


class EmbeddingClient:
    """Centralized embedding and query expansion client.

    Used by: ProviderSearchService (large model), SpecialtyService (small model + expansion)
    """

    def __init__(self):
        self._oai_client: Optional[OpenAI] = None

    def _get_oai(self) -> OpenAI:
        if self._oai_client is None:
            self._oai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        return self._oai_client

    def get_query_embedding(self, text: str) -> Optional[list]:
        """Embed via text-embedding-3-large. For provider vector search."""
        try:
            return self._get_oai().embeddings.create(model="text-embedding-3-large", input=text).data[0].embedding
        except Exception as e:
            log.warning("Embedding (large) failed: %s", e, exc=ChatHealthyException(
                                                            mode="embedding_large_failed",
                                                            message=f"Embedding (large) failed: {e}",
                                                            component="EmbeddingClient",
                                                            exception=e,
                                                        ), if_not_debug_log=True)
            return None

    def get_specialty_vector(self, text: str) -> list:
        """Embed via text-embedding-3-large. For specialty matching.
        Same model as provider embeddings — required for cross-collection RAG.
        Raises on failure (no fallback per EPIC-006-F-002-S-001-REQ-T-001).
        SpecialtyFilter's find_specialties() is the single catch point and
        surfaces the actual upstream cause to the frontend."""
        return self._get_oai().embeddings.create(
            model="text-embedding-3-large", input=text
        ).data[0].embedding

    def expand_query_terms(self, query: str) -> list[str]:
        """AI query expansion via Claude Haiku. Returns keyword stems."""
        try:
            client = Anthropic(api_key=os.getenv("Anthropic_API_KEY"))
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": (
                    "You are helping search a medical provider taxonomy database. "
                    "Given the specialty query, return a JSON array of 2-5 lowercase keyword stems "
                    "to search for in Specialization and Display Name fields. "
                    "Return ONLY the JSON array, no other text.\n\n"
                    "Examples:\n"
                    'Query: pediatrician -> ["pediatric", "child"]\n'
                    'Query: cardiologist -> ["cardio", "cardiac", "cardiovascular"]\n'
                    'Query: OB-GYN -> ["obstetric", "gynecolog", "maternal", "fetal"]\n\n'
                    f"Query: {query}"
                )}],
            )
            raw = response.content[0].text.strip() if response.content else ""
            return json.loads(raw) if raw else []
        except Exception as exc:
            log.warning("expand_query_terms failed for %r: %s", query, exc, exc=ChatHealthyException(
                                                                             mode="expand_query_terms_failed",
                                                                             message=f"expand_query_terms failed for {query!r}: {exc}",
                                                                             component="EmbeddingClient",
                                                                             exception=exc,
                                                                         ), if_not_debug_log=True)
            return []
