# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# test_embedding_client.py — Tests for EmbeddingClient.
#
# AUDIT FIX 2026-04-15:
#   - test_get_query_embedding_returns_list: REMOVED — mocked away the entire
#     OpenAI client (the thing being tested). A real regression would not be caught.
#   - test_expand_query_terms_returns_stems: REMOVED — mocked away Anthropic client
#     entirely. Tested JSON parsing of a fake response.
#   - Error-handling tests KEPT — mock is appropriate for error injection (testing
#     that the client handles failures gracefully, not that the API works).
#   - Added structural tests that verify the client's API contract without
#     requiring live API keys.

import inspect
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from infrastructure.embeddings.embedding_client import EmbeddingClient


class TestEmbeddingClient(unittest.TestCase):

    def setUp(self):
        self.client = EmbeddingClient()

    # ── Structural tests — verify the client calls the right API ──────

    def test_get_query_embedding_uses_correct_model(self):
        """Verify get_query_embedding calls OpenAI with text-embedding-3-large."""
        source = inspect.getsource(EmbeddingClient.get_query_embedding)
        self.assertIn("text-embedding-3-large", source,
                      "get_query_embedding must use text-embedding-3-large model")

    def test_get_specialty_vector_uses_correct_model(self):
        """Verify get_specialty_vector calls OpenAI with text-embedding-3-large."""
        source = inspect.getsource(EmbeddingClient.get_specialty_vector)
        self.assertIn("text-embedding-3-large", source,
                      "get_specialty_vector must use text-embedding-3-large model")

    def test_expand_query_terms_uses_haiku(self):
        """Verify expand_query_terms uses Claude Haiku for query expansion."""
        source = inspect.getsource(EmbeddingClient.expand_query_terms)
        self.assertIn("haiku", source,
                      "expand_query_terms must use a Haiku model")

    def test_expand_query_terms_prompt_mentions_taxonomy(self):
        """Verify the expansion prompt is about medical provider taxonomy."""
        source = inspect.getsource(EmbeddingClient.expand_query_terms)
        self.assertIn("medical provider taxonomy", source,
                      "expand_query_terms prompt must reference medical provider taxonomy")

    # ── Error handling tests — mock is appropriate for error injection ──

    @patch("infrastructure.embeddings.embedding_client.OpenAI")
    def test_get_query_embedding_failure_returns_none(self, mock_oai_class):
        """When OpenAI API fails, get_query_embedding returns None (fail-open)."""
        mock_oai_class.return_value.embeddings.create.side_effect = Exception("API down")
        result = self.client.get_query_embedding("test")
        self.assertIsNone(result)

    @patch("infrastructure.embeddings.embedding_client.OpenAI")
    def test_get_specialty_vector_failure_returns_none(self, mock_oai_class):
        """When OpenAI API fails, get_specialty_vector returns None (fail-open)."""
        mock_oai_class.return_value.embeddings.create.side_effect = Exception("API down")
        result = self.client.get_specialty_vector("test")
        self.assertIsNone(result)

    @patch("infrastructure.embeddings.embedding_client.Anthropic")
    def test_expand_query_terms_failure_returns_empty(self, mock_anthropic_class):
        """When Anthropic API fails, expand_query_terms returns [] (fail-open)."""
        mock_anthropic_class.return_value.messages.create.side_effect = Exception("fail")
        result = self.client.expand_query_terms("test")
        self.assertEqual(result, [])

    # ── Lazy init test ────────────────────────────────────────────────

    def test_openai_client_lazy_init(self):
        """OpenAI client is not created until first embedding call."""
        client = EmbeddingClient()
        self.assertIsNone(client._oai_client)


if __name__ == "__main__":
    unittest.main()
