# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Unit tests for gpt_reader.py
# Tests: auth, ReadArtifact, Query, caps, error codes, audit logging
# Design: brain_v0.1.3_design.json v6.0

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add DataPipelines to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gpt_reader import (
    handle_gpt_reader,
    _authenticate,
    _is_db_allowed,
    _check_response_size,
    MAX_RESPONSE_BYTES,
    MAX_QUERY_DOCUMENTS,
    DEFAULT_QUERY_LIMIT,
)

MOCK_ENV = {"MONGO_FRONTEND_connectionString": "mongodb://mock:27017", "GPT_READER_TOKEN": "test-token"}


class TestAuth(unittest.TestCase):
    """R4: Bearer token authentication."""

    @patch.dict(os.environ, {"GPT_READER_TOKEN": "test-token-123"})
    def test_valid_token(self):
        import importlib, gpt_reader
        importlib.reload(gpt_reader)
        self.assertTrue(gpt_reader._authenticate("test-token-123"))

    @patch.dict(os.environ, {"GPT_READER_TOKEN": "test-token-123"})
    def test_invalid_token(self):
        import importlib, gpt_reader
        importlib.reload(gpt_reader)
        self.assertFalse(gpt_reader._authenticate("wrong-token"))

    @patch.dict(os.environ, {"GPT_READER_TOKEN": ""})
    def test_no_token_configured(self):
        import importlib, gpt_reader
        importlib.reload(gpt_reader)
        self.assertFalse(gpt_reader._authenticate("anything"))

    @patch.dict(os.environ, {"GPT_READER_TOKEN": "test-token-123"})
    def test_auth_returns_401(self):
        import importlib, gpt_reader
        importlib.reload(gpt_reader)
        status, resp = gpt_reader.handle_gpt_reader(
            {"action": "ReadArtifact", "project_path": "test"},
            "wrong-token"
        )
        self.assertEqual(status, 401)


class TestDbAllowlist(unittest.TestCase):
    """R2: Only allowlisted databases."""

    def test_allowed_env_prefixed(self):
        self.assertTrue(_is_db_allowed("dev_PublicHealthData"))
        self.assertTrue(_is_db_allowed("qa_System"))
        self.assertTrue(_is_db_allowed("prod_Debug"))
        self.assertTrue(_is_db_allowed("dev_AboutUs"))

    def test_allowed_global(self):
        self.assertTrue(_is_db_allowed("admin"))

    def test_not_allowed(self):
        self.assertFalse(_is_db_allowed("secret_database"))
        self.assertFalse(_is_db_allowed("PublicHealthData"))
        self.assertFalse(_is_db_allowed("test_SomethingElse"))
        self.assertFalse(_is_db_allowed(""))


class TestResponseCaps(unittest.TestCase):
    """R7: Response size cap."""

    def test_under_cap(self):
        self.assertIsNone(_check_response_size("small content", "ReadArtifact"))

    def test_over_cap(self):
        result = _check_response_size("x" * (MAX_RESPONSE_BYTES + 1), "ReadArtifact")
        self.assertIsNotNone(result)
        self.assertEqual(result["error"], "response_too_large")

    def test_exactly_at_cap(self):
        self.assertIsNone(_check_response_size("x" * MAX_RESPONSE_BYTES, "Query"))


@patch.dict(os.environ, MOCK_ENV)
class TestReadArtifact(unittest.TestCase):
    """R1, R5, R13, R27: ReadArtifact action."""

    @patch("gpt_reader._authenticate", return_value=True)
    @patch("gpt_reader.MongoClient")
    def test_read_text_artifact(self, mock_mongo_class, mock_auth):
        mock_client = MagicMock()
        mock_mongo_class.return_value = mock_client
        mock_coll = MagicMock()
        mock_client.__getitem__ = MagicMock(return_value=MagicMock(__getitem__=MagicMock(return_value=mock_coll)))
        mock_coll.find_one.return_value = {
            "project_path": "brain/machine_artifacts/document_type_json/test.json",
            "mime_type": "application/json",
            "encoding": "text",
            "size": 42,
            "content": '{"test": true}',
        }
        mock_coll.insert_one = MagicMock()

        status, resp = handle_gpt_reader(
            {"action": "ReadArtifact", "project_path": "brain/machine_artifacts/document_type_json/test.json"},
            "valid-token"
        )
        self.assertEqual(status, 200)
        self.assertEqual(resp["encoding"], "text")
        self.assertEqual(resp["content"], '{"test": true}')

    @patch("gpt_reader._authenticate", return_value=True)
    @patch("gpt_reader.MongoClient")
    def test_path_traversal_blocked(self, mock_mongo_class, mock_auth):
        mock_mongo_class.return_value = MagicMock()
        status, resp = handle_gpt_reader(
            {"action": "ReadArtifact", "project_path": "../../etc/passwd"}, "valid-token"
        )
        self.assertEqual(status, 403)

    @patch("gpt_reader._authenticate", return_value=True)
    @patch("gpt_reader.MongoClient")
    def test_absolute_path_blocked(self, mock_mongo_class, mock_auth):
        mock_mongo_class.return_value = MagicMock()
        status, resp = handle_gpt_reader(
            {"action": "ReadArtifact", "project_path": "/etc/passwd"}, "valid-token"
        )
        self.assertEqual(status, 403)

    @patch("gpt_reader._authenticate", return_value=True)
    @patch("gpt_reader.MongoClient")
    def test_missing_path(self, mock_mongo_class, mock_auth):
        mock_mongo_class.return_value = MagicMock()
        status, resp = handle_gpt_reader({"action": "ReadArtifact"}, "valid-token")
        self.assertEqual(status, 400)

    @patch("gpt_reader._authenticate", return_value=True)
    @patch("gpt_reader.MongoClient")
    def test_not_found(self, mock_mongo_class, mock_auth):
        mock_client = MagicMock()
        mock_mongo_class.return_value = mock_client
        mock_coll = MagicMock()
        mock_client.__getitem__ = MagicMock(return_value=MagicMock(__getitem__=MagicMock(return_value=mock_coll)))
        mock_coll.find_one.return_value = None
        mock_coll.insert_one = MagicMock()
        status, resp = handle_gpt_reader(
            {"action": "ReadArtifact", "project_path": "nonexistent/file.json"}, "valid-token"
        )
        self.assertEqual(status, 404)

    @patch("gpt_reader._authenticate", return_value=True)
    @patch("gpt_reader.MongoClient")
    def test_snapshot_mismatch_returns_409(self, mock_mongo_class, mock_auth):
        mock_client = MagicMock()
        mock_mongo_class.return_value = mock_client
        mock_coll = MagicMock()
        mock_client.__getitem__ = MagicMock(return_value=MagicMock(__getitem__=MagicMock(return_value=mock_coll)))
        mock_coll.find_one.side_effect = [
            None,
            {"project_path": "test.json", "manifest_snapshot_id": "old_snapshot"},
        ]
        mock_coll.insert_one = MagicMock()
        status, resp = handle_gpt_reader(
            {"action": "ReadArtifact", "project_path": "test.json", "manifest_snapshot_id": "new_snapshot"},
            "valid-token"
        )
        self.assertEqual(status, 409)


@patch.dict(os.environ, MOCK_ENV)
class TestQuery(unittest.TestCase):
    """R2, R3, R7, R8, R14: Query action."""

    @patch("gpt_reader._authenticate", return_value=True)
    @patch("gpt_reader.MongoClient")
    def test_missing_database(self, mock_mongo_class, mock_auth):
        mock_mongo_class.return_value = MagicMock()
        status, resp = handle_gpt_reader(
            {"action": "Query", "collection": "providers"}, "valid-token"
        )
        self.assertEqual(status, 400)

    @patch("gpt_reader._authenticate", return_value=True)
    @patch("gpt_reader.MongoClient")
    def test_unauthorized_database(self, mock_mongo_class, mock_auth):
        mock_client = MagicMock()
        mock_mongo_class.return_value = mock_client
        mock_coll = MagicMock()
        mock_client.__getitem__ = MagicMock(return_value=MagicMock(__getitem__=MagicMock(return_value=mock_coll)))
        mock_coll.insert_one = MagicMock()
        status, resp = handle_gpt_reader(
            {"action": "Query", "database": "secret_db", "collection": "data"}, "valid-token"
        )
        self.assertEqual(status, 403)

    @patch("gpt_reader._authenticate", return_value=True)
    @patch("gpt_reader.MongoClient")
    def test_limit_capped(self, mock_mongo_class, mock_auth):
        mock_client = MagicMock()
        mock_mongo_class.return_value = mock_client
        mock_coll = MagicMock()
        mock_client.__getitem__ = MagicMock(return_value=MagicMock(__getitem__=MagicMock(return_value=mock_coll)))
        mock_coll.count_documents.return_value = 5
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.limit.return_value = iter([{"_id": "1", "test": True}])
        mock_coll.find.return_value = mock_cursor
        mock_coll.insert_one = MagicMock()

        status, resp = handle_gpt_reader(
            {"action": "Query", "database": "dev_PublicHealthData", "collection": "providers",
             "query": {}, "limit": 500}, "valid-token"
        )
        self.assertEqual(status, 200)
        mock_cursor.limit.assert_called_with(MAX_QUERY_DOCUMENTS)

    @patch("gpt_reader._authenticate", return_value=True)
    @patch("gpt_reader.MongoClient")
    def test_507_on_too_many_documents(self, mock_mongo_class, mock_auth):
        mock_client = MagicMock()
        mock_mongo_class.return_value = mock_client
        mock_coll = MagicMock()
        mock_client.__getitem__ = MagicMock(return_value=MagicMock(__getitem__=MagicMock(return_value=mock_coll)))
        mock_coll.count_documents.return_value = 150000
        mock_coll.insert_one = MagicMock()

        status, resp = handle_gpt_reader(
            {"action": "Query", "database": "dev_PublicHealthData", "collection": "providers",
             "query": {}}, "valid-token"
        )
        self.assertEqual(status, 507)
        self.assertEqual(resp["error"], "response_too_large")
        self.assertIn("estimated_result", resp)

    @patch("gpt_reader._authenticate", return_value=True)
    @patch("gpt_reader.MongoClient")
    def test_invalid_action(self, mock_mongo_class, mock_auth):
        mock_mongo_class.return_value = MagicMock()
        status, resp = handle_gpt_reader({"action": "DeleteEverything"}, "valid-token")
        self.assertEqual(status, 400)


class TestConstants(unittest.TestCase):
    """Verify design constants match requirements."""

    def test_max_response_500kb(self):
        self.assertEqual(MAX_RESPONSE_BYTES, 500 * 1024)

    def test_max_query_docs_100(self):
        self.assertEqual(MAX_QUERY_DOCUMENTS, 100)

    def test_default_limit_10(self):
        self.assertEqual(DEFAULT_QUERY_LIMIT, 10)


if __name__ == "__main__":
    unittest.main()
