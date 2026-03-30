# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Integration tests for gpt_reader.py — hits real MongoDB.
# Simulates GPT calling the service.
# Requires: MONGO_FRONTEND_connectionString and GPT_READER_TOKEN in .env
#
# Run: python -m pytest Code/DataPipelines/tests/test_gpt_reader_integration.py -v
#
# Design: brain_v0.1.3_design.json v6.0
# Tests every requirement with real data.

import json
import os
import sys
import unittest

from dotenv import load_dotenv

# Load .env
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

# Add DataPipelines to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TOKEN = os.environ.get("GPT_READER_TOKEN", "")


def gpt_call(config: dict) -> tuple:
    """Simulate GPT calling the service. Reloads module to ensure token is set."""
    import importlib
    import gpt_reader
    importlib.reload(gpt_reader)
    return gpt_reader.handle_gpt_reader(config, TOKEN)


class TestR4_Authentication(unittest.TestCase):
    """R4: Bearer token authentication, separate from Router token."""

    def test_valid_token_accepted(self):
        status, resp = gpt_call({"action": "ReadArtifact", "project_path": "brain/manifest/project_manifest.json"})
        self.assertNotEqual(status, 401)

    def test_invalid_token_rejected(self):
        import importlib, gpt_reader
        importlib.reload(gpt_reader)
        status, resp = gpt_reader.handle_gpt_reader({"action": "ReadArtifact", "project_path": "test"}, "wrong-token")
        self.assertEqual(status, 401)

    def test_empty_token_rejected(self):
        import importlib, gpt_reader
        importlib.reload(gpt_reader)
        status, resp = gpt_reader.handle_gpt_reader({"action": "ReadArtifact", "project_path": "test"}, "")
        self.assertEqual(status, 401)


class TestR1_ReadArtifact_Text(unittest.TestCase):
    """R1: GPT can read any file in the project manifest.
       R5: Text files returned as text."""

    def test_read_json_artifact(self):
        status, resp = gpt_call({
            "action": "ReadArtifact",
            "project_path": "brain/machine_artifacts/document_type_json/corporate_governance.json"
        })
        self.assertEqual(status, 200)
        self.assertEqual(resp["encoding"], "text")
        self.assertEqual(resp["mime_type"], "application/json")
        self.assertIn("content", resp)
        self.assertIn("ChatHealthy", resp["content"])

    def test_read_md_artifact(self):
        status, resp = gpt_call({
            "action": "ReadArtifact",
            "project_path": "brain/machine_artifacts/document_type_md/qa_report_v012_final.md"
        })
        self.assertEqual(status, 200)
        self.assertEqual(resp["encoding"], "text")
        self.assertIn("content", resp)

    def test_read_py_artifact(self):
        status, resp = gpt_call({
            "action": "ReadArtifact",
            "project_path": "Code/DataPipelines/gpt_reader.py"
        })
        self.assertEqual(status, 200)
        self.assertEqual(resp["encoding"], "text")
        self.assertIn("handle_gpt_reader", resp["content"])


class TestR5_ReadArtifact_Binary(unittest.TestCase):
    """R5: Binary files returned as base64."""

    def test_read_pdf_artifact(self):
        status, resp = gpt_call({
            "action": "ReadArtifact",
            "project_path": "brain/BusinessArtifacts/qa_report_v012_final.pdf"
        })
        self.assertEqual(status, 200)
        self.assertEqual(resp["encoding"], "base64")
        self.assertIn("application/pdf", resp["mime_type"])
        # Base64 content should be decodable
        import base64
        decoded = base64.b64decode(resp["content"])
        self.assertTrue(decoded.startswith(b"%PDF"))


class TestR6_ManifestFields(unittest.TestCase):
    """R6: Every manifest entry includes project_path, mime_type, encoding, size, content_hash."""

    def test_manifest_has_required_fields(self):
        from pymongo import MongoClient
        conn = os.environ.get("MONGO_FRONTEND_connectionString")
        client = MongoClient(conn)
        manifest = client["admin"]["manifest"].find_one({"_id": "current"})
        self.assertIsNotNone(manifest)
        self.assertIn("entries", manifest)
        self.assertIn("snapshot_id", manifest)

        for entry in manifest["entries"][:5]:  # Sample first 5
            self.assertIn("project_path", entry, f"Missing project_path in {entry.get('filename')}")
            self.assertIn("mime_type", entry, f"Missing mime_type in {entry.get('filename')}")
            self.assertIn("encoding", entry, f"Missing encoding in {entry.get('filename')}")
            self.assertIn("size", entry, f"Missing size in {entry.get('filename')}")
            self.assertIn("content_hash", entry, f"Missing content_hash in {entry.get('filename')}")
        client.close()


class TestR2_QueryAllowlist(unittest.TestCase):
    """R2: GPT can query only allowlisted databases."""

    def test_allowed_database(self):
        status, resp = gpt_call({
            "action": "Query",
            "database": "dev_PublicHealthData",
            "collection": "SpecialtyMetaData",
            "query": {},
            "limit": 5
        })
        self.assertEqual(status, 200)
        self.assertGreater(resp["count"], 0)

    def test_admin_allowed(self):
        status, resp = gpt_call({
            "action": "Query",
            "database": "admin",
            "collection": "framework_version",
            "query": {},
            "limit": 5
        })
        self.assertEqual(status, 200)

    def test_unauthorized_database_returns_403(self):
        status, resp = gpt_call({
            "action": "Query",
            "database": "secret_database",
            "collection": "data",
            "query": {},
            "limit": 5
        })
        self.assertEqual(status, 403)


class TestR3_ReadOnly(unittest.TestCase):
    """R3: All access is read-only. Only ReadArtifact and Query are valid actions."""

    def test_unknown_action_rejected(self):
        status, resp = gpt_call({"action": "InsertDocument", "database": "admin", "collection": "test"})
        self.assertEqual(status, 400)

    def test_delete_action_rejected(self):
        status, resp = gpt_call({"action": "Delete", "database": "admin", "collection": "test"})
        self.assertEqual(status, 400)


class TestR7_ResponseCap(unittest.TestCase):
    """R7: Response cap 500KB."""

    def test_small_query_under_cap(self):
        status, resp = gpt_call({
            "action": "Query",
            "database": "dev_PublicHealthData",
            "collection": "SpecialtyMetaData",
            "query": {},
            "limit": 5
        })
        self.assertEqual(status, 200)


class TestR8_QueryLimit(unittest.TestCase):
    """R8: Query cap 100 documents, default 10."""

    def test_default_limit_applied(self):
        status, resp = gpt_call({
            "action": "Query",
            "database": "dev_PublicHealthData",
            "collection": "SpecialtyMetaData",
            "query": {}
            # No limit specified — should default to 10
        })
        self.assertEqual(status, 200)
        self.assertLessEqual(resp["count"], 10)

    def test_explicit_limit_respected(self):
        status, resp = gpt_call({
            "action": "Query",
            "database": "dev_PublicHealthData",
            "collection": "SpecialtyMetaData",
            "query": {},
            "limit": 3
        })
        self.assertEqual(status, 200)
        self.assertLessEqual(resp["count"], 3)


class TestR13_PathBoundary(unittest.TestCase):
    """R13: No access outside project boundary."""

    def test_path_traversal_blocked(self):
        status, resp = gpt_call({
            "action": "ReadArtifact",
            "project_path": "../../etc/passwd"
        })
        self.assertEqual(status, 403)

    def test_absolute_path_blocked(self):
        status, resp = gpt_call({
            "action": "ReadArtifact",
            "project_path": "/etc/passwd"
        })
        self.assertEqual(status, 403)


class TestR14_507Body(unittest.TestCase):
    """R14: 507 on too many documents with helpful body."""

    def test_507_on_large_query(self):
        status, resp = gpt_call({
            "action": "Query",
            "database": "dev_PublicHealthData",
            "collection": "providers",
            "query": {}  # 143K docs, no filter
        })
        self.assertEqual(status, 507)
        self.assertEqual(resp["error"], "response_too_large")
        self.assertIn("suggestion", resp)
        # 507 can fire on document count OR response bytes — both are valid
        self.assertTrue(
            "estimated_result" in resp or "actual_bytes" in resp,
            "507 body should include either estimated_result or actual_bytes"
        )


class TestR19_FunctionTimeout(unittest.TestCase):
    """R19: Verify function completes within reasonable time."""

    def test_read_artifact_fast(self):
        import time
        start = time.time()
        status, resp = gpt_call({
            "action": "ReadArtifact",
            "project_path": "brain/machine_artifacts/document_type_json/corporate_governance.json"
        })
        elapsed = time.time() - start
        self.assertEqual(status, 200)
        self.assertLess(elapsed, 30, "ReadArtifact should complete well under 30s timeout")

    def test_query_fast(self):
        import time
        start = time.time()
        status, resp = gpt_call({
            "action": "Query",
            "database": "dev_PublicHealthData",
            "collection": "SpecialtyMetaData",
            "query": {},
            "limit": 10
        })
        elapsed = time.time() - start
        self.assertEqual(status, 200)
        self.assertLess(elapsed, 30, "Query should complete well under 30s timeout")


class TestR26_Audit(unittest.TestCase):
    """R26: Every read is audited."""

    def test_audit_log_written(self):
        from pymongo import MongoClient
        conn = os.environ.get("MONGO_FRONTEND_connectionString")
        client = MongoClient(conn)

        # Count audit entries before
        before = client["admin"]["gpt_reader_audit"].count_documents({})

        # Make a call
        gpt_call({
            "action": "ReadArtifact",
            "project_path": "brain/machine_artifacts/document_type_json/corporate_governance.json"
        })

        # Count after
        after = client["admin"]["gpt_reader_audit"].count_documents({})
        self.assertGreater(after, before, "Audit log should have a new entry")

        # Check latest entry has required fields
        latest = client["admin"]["gpt_reader_audit"].find_one(sort=[("timestamp", -1)])
        self.assertIn("actor", latest)
        self.assertIn("action", latest)
        self.assertIn("target", latest)
        self.assertIn("timestamp", latest)
        self.assertIn("status_code", latest)
        client.close()


class TestR27_SnapshotConsistency(unittest.TestCase):
    """R27: Manifest snapshots include snapshot ID. Stale returns 409."""

    def test_snapshot_id_in_manifest(self):
        from pymongo import MongoClient
        conn = os.environ.get("MONGO_FRONTEND_connectionString")
        client = MongoClient(conn)
        manifest = client["admin"]["manifest"].find_one({"_id": "current"})
        self.assertIn("snapshot_id", manifest)
        self.assertTrue(manifest["snapshot_id"].startswith("snapshot_"))
        client.close()

    def test_stale_snapshot_returns_409(self):
        status, resp = gpt_call({
            "action": "ReadArtifact",
            "project_path": "brain/machine_artifacts/document_type_json/corporate_governance.json",
            "manifest_snapshot_id": "snapshot_fake_stale_id"
        })
        self.assertEqual(status, 409)


class TestQueryMetrics(unittest.TestCase):
    """Verify query response includes metrics per design."""

    def test_metrics_in_response(self):
        status, resp = gpt_call({
            "action": "Query",
            "database": "dev_PublicHealthData",
            "collection": "SpecialtyMetaData",
            "query": {},
            "limit": 5
        })
        self.assertEqual(status, 200)
        self.assertIn("metrics", resp)
        metrics = resp["metrics"]
        self.assertIn("matched_estimate", metrics)
        self.assertIn("returned_count", metrics)
        self.assertIn("response_bytes", metrics)
        self.assertIn("elapsed_ms", metrics)


class TestGPTWorkflow(unittest.TestCase):
    """Simulates GPT's actual workflow: read manifest, then read files and query data."""

    def test_full_gpt_session_simulation(self):
        """Simulate: GPT reads manifest -> reads governance -> queries providers."""

        # Step 1: GPT reads manifest
        status, manifest_resp = gpt_call({
            "action": "ReadArtifact",
            "project_path": "brain/manifest/project_manifest.json"
        })
        self.assertEqual(status, 200, "GPT should be able to read the manifest")
        manifest = json.loads(manifest_resp["content"])
        self.assertIsInstance(manifest, list)
        self.assertGreater(len(manifest), 0, "Manifest should have entries")

        # Step 2: GPT finds governance file in manifest
        gov_entries = [e for e in manifest if "corporate_governance" in e.get("project_path", "")]
        self.assertEqual(len(gov_entries), 1, "Should find exactly one governance file")

        # Step 3: GPT reads governance
        status, gov_resp = gpt_call({
            "action": "ReadArtifact",
            "project_path": gov_entries[0]["project_path"]
        })
        self.assertEqual(status, 200, "GPT should be able to read governance")
        gov = json.loads(gov_resp["content"])
        self.assertIn("policies", gov, "Governance should have policies")

        # Step 4: GPT queries providers
        status, query_resp = gpt_call({
            "action": "Query",
            "database": "dev_PublicHealthData",
            "collection": "providers",
            "query": {"practice_address.state": "DE"},
            "projection": {"npi": 1, "provider_last_name_legal_name": 1},
            "limit": 5
        })
        self.assertEqual(status, 200, "GPT should be able to query providers")
        self.assertGreater(query_resp["count"], 0, "Should find DE providers")

        # Step 5: GPT queries framework version
        status, fw_resp = gpt_call({
            "action": "Query",
            "database": "admin",
            "collection": "framework_version",
            "query": {"to": None},
            "limit": 1
        })
        self.assertEqual(status, 200, "GPT should be able to read framework version")


if __name__ == "__main__":
    unittest.main()
