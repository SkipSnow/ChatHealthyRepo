# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
import os, sys, unittest
from unittest.mock import patch, MagicMock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from infrastructure.embeddings.embedding_client import EmbeddingClient


class TestEmbeddingClient(unittest.TestCase):

    def setUp(self):
        self.client = EmbeddingClient()

    @patch("infrastructure.embeddings.embedding_client.OpenAI")
    def test_get_query_embedding_returns_list(self, mock_oai_class):
        mock_client = MagicMock()
        mock_oai_class.return_value = mock_client
        mock_client.embeddings.create.return_value.data = [MagicMock(embedding=[0.1, 0.2, 0.3])]
        result = self.client.get_query_embedding("cardiologist")
        self.assertEqual(result, [0.1, 0.2, 0.3])

    @patch("infrastructure.embeddings.embedding_client.OpenAI")
    def test_get_query_embedding_failure_returns_none(self, mock_oai_class):
        mock_oai_class.return_value.embeddings.create.side_effect = Exception("API down")
        result = self.client.get_query_embedding("test")
        self.assertIsNone(result)

    @patch("infrastructure.embeddings.embedding_client.OpenAI")
    def test_get_specialty_vector_returns_list(self, mock_oai_class):
        mock_client = MagicMock()
        mock_oai_class.return_value = mock_client
        mock_client.embeddings.create.return_value.data = [MagicMock(embedding=[0.4, 0.5])]
        result = self.client.get_specialty_vector("pediatrics")
        self.assertEqual(result, [0.4, 0.5])

    @patch("infrastructure.embeddings.embedding_client.Anthropic")
    def test_expand_query_terms_returns_stems(self, mock_anthropic_class):
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value.content = [MagicMock(text='["cardio", "cardiac"]')]
        result = self.client.expand_query_terms("cardiologist")
        self.assertEqual(result, ["cardio", "cardiac"])

    @patch("infrastructure.embeddings.embedding_client.Anthropic")
    def test_expand_query_terms_failure_returns_empty(self, mock_anthropic_class):
        mock_anthropic_class.return_value.messages.create.side_effect = Exception("fail")
        result = self.client.expand_query_terms("test")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
